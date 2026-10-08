"""检索与问答编排：关键字 / 向量 / 混合，无命中不调用对话模型。"""
from __future__ import annotations

import re
import sqlite3

from .chunking import parse_strategy_fields, split_text
from .config import Config
from .db import get_connection
from .embedding import EmbeddingClient, EmbeddingUnavailable
from .vector_store import VectorStore, VectorUnavailable

VECTOR_MIN_SCORE = 0.35
RRF_K = 60
CANDIDATES = 8
ASK_TOP = 4
EXCERPT_LIMIT = 160
NOT_FOUND = "资料中未找到相关内容"
HISTORY_LIMIT = 6

SYSTEM_PROMPT = (
    "你是本班教研资料助手。只根据用户消息里给出的资料切片回答，"
    "回答要简短，并在依据处用 [1]、[2] 这类序号标注，序号与资料编号一致。"
    "资料没有写到的内容不要编造。"
)

_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
_CITE_RE = re.compile(r"\[(\d+)\]")


class RetrievalUnavailable(Exception):
    """向量库或嵌入 / 对话网关不可用。调用方返回同形 503。"""


def search(config: Config, class_id: int, query: str, mode: str) -> dict:
    """班级只使用调用方传入的会话班级。"""
    query = (query or "").strip()
    if not query:
        raise ValueError("查询不能为空")
    mode = (mode or "hybrid").strip().lower()
    if mode not in ("keyword", "vector", "hybrid"):
        raise ValueError("mode 须为 keyword、vector 或 hybrid")
    if mode == "keyword":
        hits = keyword_search(class_id, query, limit=CANDIDATES)
    elif mode == "vector":
        hits = vector_search(config, class_id, query, limit=CANDIDATES)
    else:
        hits = hybrid_search(config, class_id, query, limit=CANDIDATES)
    return {"mode": mode, "hits": [_public_hit(hit) for hit in hits]}


def keyword_search(class_id: int, query: str, limit: int = CANDIDATES) -> list[dict]:
    """词元计数评分。不调用嵌入，不访问向量库。"""
    tokens = _tokens(query)
    if not tokens:
        return []
    clauses = " OR ".join("c.chunk_text LIKE ? ESCAPE '\\'" for _ in tokens)
    likes = [_like(token) for token in tokens]
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.chunk_index, c.chunk_text, c.char_start, c.char_end,
                   c.material_id, m.title AS material_title
            FROM knowledge_chunks c
            JOIN materials m ON m.id = c.material_id
            WHERE c.class_id = ? AND c.embed_status = 'ready' AND ({clauses})
            """,
            (class_id, *likes),
        ).fetchall()
    finally:
        conn.close()
    scored = []
    for row in rows:
        text = row["chunk_text"]
        lowered = text.lower()
        score = 0
        for token in tokens:
            score += lowered.count(token.lower()) * len(token)
        if score <= 0:
            continue
        scored.append(_row_hit(row, score))
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return scored[:limit]


def vector_search(config: Config, class_id: int, query: str, limit: int = CANDIDATES) -> list[dict]:
    vector = _embed_query(config, query)
    try:
        points = VectorStore(config).search(vector, class_id, limit, VECTOR_MIN_SCORE)
    except VectorUnavailable as exc:
        raise RetrievalUnavailable("向量库不可用") from exc
    points = [point for point in points if point["score"] >= VECTOR_MIN_SCORE]
    points.sort(key=lambda point: -point["score"])
    return _hydrate(class_id, points)


def hybrid_search(config: Config, class_id: int, query: str, limit: int = CANDIDATES) -> list[dict]:
    keyword_hits = keyword_search(class_id, query, limit=CANDIDATES)
    vector_hits = vector_search(config, class_id, query, limit=CANDIDATES)
    fused = fuse_rrf(keyword_hits, vector_hits)
    return fused[:limit]


def fuse_rrf(keyword_hits: list[dict], vector_hits: list[dict], k: int = RRF_K) -> list[dict]:
    """按名次融合。缺席的一路不加分，两路分数不相加原值。"""
    scores: dict[int, float] = {}
    items: dict[int, dict] = {}
    for rank, hit in enumerate(keyword_hits, start=1):
        chunk_id = int(hit["chunk_id"])
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        items[chunk_id] = hit
    for rank, hit in enumerate(vector_hits, start=1):
        chunk_id = int(hit["chunk_id"])
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        items[chunk_id] = hit
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    fused = []
    for chunk_id in ordered:
        hit = dict(items[chunk_id])
        hit["score"] = scores[chunk_id]
        fused.append(hit)
    return fused


def ask(config: Config, class_id: int, question: str, messages: list | None) -> dict:
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    history = _clean_history(messages or [], question)
    hits = hybrid_search(config, class_id, question, limit=ASK_TOP)
    return finish_ask(config, question, hits, history)


def finish_ask(config: Config, question: str, hits: list[dict], history: list[dict]) -> dict:
    """有命中才调用对话网关。空命中直接返回固定文案。"""
    top = hits[:ASK_TOP]
    if not top:
        return {"answer": NOT_FOUND, "citations": []}
    if not config.chat_configured():
        raise RetrievalUnavailable("对话网关未配置")
    prompt_messages = build_chat_messages(question, top, history)
    raw = _chat_complete(config, prompt_messages)
    answer, citations = align_citations(raw, top)
    return {"answer": answer, "citations": citations}


def build_chat_messages(question: str, hits: list[dict], history: list[dict]) -> list[dict]:
    """system 由服务端固定写入。上下文只有标题、切片序号、正文和对话。"""
    blocks = []
    for i, hit in enumerate(hits, start=1):
        blocks.append(
            f"[{i}] 材料：{hit['material_title']} 切片 {hit['chunk_index']}\n{hit['chunk_text']}"
        )
    user_content = (
        "请只依据下列资料回答问题，用到某条资料时标出对应序号。\n\n"
        + "\n\n".join(blocks)
        + f"\n\n问题：{question}"
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})
    return messages


def align_citations(answer: str, hits: list[dict]) -> tuple[str, list[dict]]:
    """按回答里 [n] 的首次出现顺序重排，并重编号，使 [1] 对应 citations[0]。"""
    found: list[int] = []
    for match in _CITE_RE.finditer(answer or ""):
        number = int(match.group(1))
        if 1 <= number <= len(hits) and number not in found:
            found.append(number)
    if not found:
        found = list(range(1, len(hits) + 1))
        if found:
            answer = (answer or "").rstrip() + " " + " ".join(f"[{n}]" for n in found)
    remap = {old: new for new, old in enumerate(found, start=1)}

    def repl(match: re.Match) -> str:
        number = int(match.group(1))
        if number in remap:
            return f"[{remap[number]}]"
        return ""

    rewritten = _CITE_RE.sub(repl, answer).strip()
    citations = [_public_hit(hits[number - 1]) for number in found]
    return rewritten, citations


def index_material(
    config: Config,
    *,
    material_id: int,
    class_id: int,
    entry_id: int,
    body_text: str,
    strategy: str,
    options: dict | None = None,
) -> dict:
    """切分并写入切片。嵌入失败的切片保持 failed，不写不完整向量。"""
    _source, chunks = split_text(body_text, strategy, options or {})
    conn = get_connection()
    ids: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for chunk in chunks:
            cur = conn.execute(
                """
                INSERT INTO knowledge_chunks
                    (material_id, class_id, chunk_index, chunk_text, char_start, char_end, embed_status)
                VALUES (?,?,?,?,?,?, 'failed')
                """,
                (material_id, class_id, chunk.index, chunk.text, chunk.char_start, chunk.char_end),
            )
            ids.append(int(cur.lastrowid))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    ready = 0
    failed = 0
    embedder = EmbeddingClient(config)
    store = VectorStore(config)
    if not embedder.configured():
        return {"chunks_total": len(chunks), "chunks_failed": len(chunks), "chunks_ready": 0}
    try:
        store.ensure_collection()
    except VectorUnavailable as exc:
        print(f"[INDEX] 向量集合不可用，切片保持 failed: {exc}")
        return {"chunks_total": len(chunks), "chunks_failed": len(chunks), "chunks_ready": 0}

    for chunk_id, chunk in zip(ids, chunks):
        try:
            vector = embedder.embed(chunk.text)
            if len(vector) != config.embedding_dim:
                raise EmbeddingUnavailable("嵌入维度与 EMBEDDING_DIM 不一致")
            store.upsert(chunk_id, vector, {
                "class_id": class_id,
                "material_id": material_id,
                "entry_id": entry_id,
                "chunk_id": chunk_id,
                "chunk_index": chunk.index,
            })
        except (EmbeddingUnavailable, VectorUnavailable) as exc:
            print(f"[INDEX] chunk {chunk_id} failed: {exc}")
            try:
                store.delete_points([chunk_id])
            except VectorUnavailable:
                pass
            failed += 1
            continue
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE knowledge_chunks SET embed_status = 'ready' WHERE id = ?",
                (chunk_id,),
            )
        finally:
            conn.close()
        ready += 1
    return {"chunks_total": len(chunks), "chunks_failed": failed, "chunks_ready": ready}


def reindex_material(
    config: Config,
    *,
    material_id: int,
    class_id: int,
    entry_id: int,
    body_text: str,
    strategy: str,
    options: dict | None = None,
) -> dict:
    """先删旧向量点与旧切片，再按本次策略重切。"""
    conn = get_connection()
    try:
        ready = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE material_id = ? AND embed_status = 'ready'",
            (material_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    store = VectorStore(config)
    try:
        store.delete_material(material_id)
    except VectorUnavailable as exc:
        if ready:
            raise RetrievalUnavailable("向量库不可用，未能删除旧索引") from exc
    conn = get_connection()
    try:
        conn.execute("DELETE FROM knowledge_chunks WHERE material_id = ?", (material_id,))
    finally:
        conn.close()
    return index_material(
        config,
        material_id=material_id,
        class_id=class_id,
        entry_id=entry_id,
        body_text=body_text,
        strategy=strategy,
        options=options,
    )


def retry_failed_chunks(config: Config) -> int:
    """把仍为 failed 的切片重新嵌入并写入向量库。依赖不可用时保持 failed。"""
    embedder = EmbeddingClient(config)
    if not embedder.configured():
        return 0
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT c.id AS chunk_id, c.chunk_text AS chunk_text, c.class_id AS class_id,
                   c.material_id AS material_id, c.chunk_index AS chunk_index,
                   ke.id AS entry_id
            FROM knowledge_chunks c
            JOIN knowledge_entries ke ON ke.material_id = c.material_id
            WHERE c.embed_status = 'failed'
            ORDER BY c.id
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0
    store = VectorStore(config)
    try:
        store.ensure_collection()
    except VectorUnavailable as exc:
        print(f"[INDEX] 向量集合不可用，跳过补嵌入: {exc}")
        return 0
    ready = 0
    for row in rows:
        chunk_id = int(row["chunk_id"])
        try:
            vector = embedder.embed(row["chunk_text"])
            if len(vector) != config.embedding_dim:
                raise EmbeddingUnavailable("嵌入维度与 EMBEDDING_DIM 不一致")
            store.upsert(chunk_id, vector, {
                "class_id": int(row["class_id"]),
                "material_id": int(row["material_id"]),
                "entry_id": int(row["entry_id"]),
                "chunk_id": chunk_id,
                "chunk_index": int(row["chunk_index"]),
            })
        except (EmbeddingUnavailable, VectorUnavailable) as exc:
            print(f"[INDEX] chunk {chunk_id} failed: {exc}")
            try:
                store.delete_points([chunk_id])
            except VectorUnavailable:
                pass
            continue
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE knowledge_chunks SET embed_status = 'ready' WHERE id = ?",
                (chunk_id,),
            )
        finally:
            conn.close()
        ready += 1
    if ready:
        print(f"[INDEX] 补嵌入完成 ready={ready} still_failed={len(rows) - ready}")
    return ready


def backfill_unchunked(config: Config) -> int:
    """仅为还没有切片的材料按 auto 补索引，不改已切分的材料。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT m.id AS material_id, m.class_id AS class_id,
                   ke.id AS entry_id, ke.content AS content
            FROM materials m
            JOIN knowledge_entries ke ON ke.material_id = m.id
            WHERE NOT EXISTS (
                SELECT 1 FROM knowledge_chunks c WHERE c.material_id = m.id
            )
            """
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        stats = index_material(
            config,
            material_id=row["material_id"],
            class_id=row["class_id"],
            entry_id=row["entry_id"],
            body_text=row["content"],
            strategy="auto",
            options={},
        )
        print(
            f"[INDEX] material {row['material_id']} "
            f"chunks={stats['chunks_total']} failed={stats['chunks_failed']}"
        )
    return len(rows)


def fields_from_mapping(data: dict) -> dict[str, str]:
    """把 JSON / 表单值收成字符串字段，并丢掉班级编号。"""
    fields: dict[str, str] = {}
    for key, value in data.items():
        if key in ("class_id", "classId"):
            continue
        if isinstance(value, bool):
            fields[key] = "true" if value else "false"
        elif value is None:
            continue
        else:
            fields[key] = str(value)
    return fields


def parse_strategy_request(data: dict) -> tuple[str, dict]:
    return parse_strategy_fields(fields_from_mapping(data))


def _embed_query(config: Config, query: str) -> list[float]:
    try:
        return EmbeddingClient(config).embed(query)
    except EmbeddingUnavailable as exc:
        raise RetrievalUnavailable("嵌入网关不可用") from exc


def _hydrate(class_id: int, points: list[dict]) -> list[dict]:
    """回表再次按会话班级约束。向量库里的他班点取不到正文。"""
    if not points:
        return []
    ids = [int(point["id"]) for point in points]
    placeholders = ",".join("?" for _ in ids)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.chunk_index, c.chunk_text, c.char_start, c.char_end,
                   c.material_id, m.title AS material_title
            FROM knowledge_chunks c
            JOIN materials m ON m.id = c.material_id
            WHERE c.id IN ({placeholders}) AND c.class_id = ? AND c.embed_status = 'ready'
            """,
            (*ids, class_id),
        ).fetchall()
    finally:
        conn.close()
    by_id = {int(row["chunk_id"]): row for row in rows}
    hits = []
    for point in points:
        row = by_id.get(int(point["id"]))
        if row is None:
            continue
        hits.append(_row_hit(row, point["score"]))
    return hits


def _row_hit(row: sqlite3.Row, score: float) -> dict:
    return {
        "material_id": row["material_id"],
        "material_title": row["material_title"],
        "chunk_id": row["chunk_id"],
        "chunk_index": row["chunk_index"],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
        "chunk_text": row["chunk_text"],
        "score": score,
    }


def _public_hit(hit: dict) -> dict:
    excerpt = _excerpt(hit["chunk_text"])
    return {
        "material_id": hit["material_id"],
        "material_title": hit["material_title"],
        "chunk_id": hit["chunk_id"],
        "chunk_index": hit["chunk_index"],
        "char_start": hit["char_start"],
        "char_end": hit["char_end"],
        "excerpt": excerpt,
        "score": hit["score"],
    }


def _excerpt(chunk_text: str) -> str:
    if len(chunk_text) <= EXCERPT_LIMIT:
        return chunk_text
    return chunk_text[:EXCERPT_LIMIT] + "…"


def _tokens(query: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(query):
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _clean_history(messages: list, question: str) -> list[dict]:
    cleaned = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        cleaned.append({"role": role, "content": text[:4000]})
    if cleaned and cleaned[-1]["role"] == "user" and cleaned[-1]["content"] == question:
        cleaned.pop()
    return cleaned[-HISTORY_LIMIT:]


def _chat_complete(config: Config, messages: list[dict]) -> str:
    import json
    import urllib.error
    import urllib.request

    url = config.chat_base_url + "/chat/completions"
    payload = {
        "model": config.chat_model,
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.chat_api_key,
        },
        method="POST",
    )
    print("[CHAT] POST /chat/completions")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise RetrievalUnavailable("对话网关返回空回答")
        return content.strip()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
        raise RetrievalUnavailable("对话网关不可用") from exc

