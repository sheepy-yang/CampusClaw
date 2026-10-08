"""Qdrant REST 适配。集合只存向量与标识，不含正文。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import Config

COLLECTION = "campusclaw_chunks"
PAYLOAD_KEYS = ("class_id", "material_id", "entry_id", "chunk_id", "chunk_index")


class VectorUnavailable(Exception):
    """向量库不可达，或集合维度与配置不一致。"""


class VectorStore:
    def __init__(self, config: Config):
        self._config = config
        self._base = config.qdrant_url.rstrip("/")

    def ensure_collection(self) -> None:
        """启动时幂等建集合。维度取 EMBEDDING_DIM；未配置嵌入时跳过。"""
        if not self._config.embedding_configured():
            print("[QDRANT] 未配置嵌入维度，跳过建集合")
            return
        dim = self._config.embedding_dim
        existing = self._get_collection()
        if existing is None:
            self._request(
                "PUT",
                f"/collections/{COLLECTION}",
                {"vectors": {"size": dim, "distance": "Cosine"}},
            )
            print(f"[QDRANT] 已创建集合 {COLLECTION} dim={dim}")
            return
        got = _collection_dim(existing)
        if got != dim:
            raise VectorUnavailable(f"集合维度 {got} 与 EMBEDDING_DIM {dim} 不一致")
        print(f"[QDRANT] 集合已存在 dim={got}")

    def upsert(self, point_id: int, vector: list[float], payload: dict) -> None:
        clean = {key: int(payload[key]) for key in PAYLOAD_KEYS}
        if len(vector) != self._config.embedding_dim:
            raise VectorUnavailable("拒绝写入维度不一致的向量")
        self._request(
            "PUT",
            f"/collections/{COLLECTION}/points?wait=true",
            {"points": [{"id": int(point_id), "vector": vector, "payload": clean}]},
        )

    def delete_points(self, point_ids: list[int]) -> None:
        if not point_ids:
            return
        self._request(
            "POST",
            f"/collections/{COLLECTION}/points/delete?wait=true",
            {"points": [int(point_id) for point_id in point_ids]},
        )

    def delete_material(self, material_id: int) -> None:
        self._request(
            "POST",
            f"/collections/{COLLECTION}/points/delete?wait=true",
            {"filter": {"must": [{"key": "material_id", "match": {"value": int(material_id)}}]}},
        )

    def search(self, vector: list[float], class_id: int, limit: int, score_threshold: float) -> list[dict]:
        print("[QDRANT] POST points/search")
        body = self._request(
            "POST",
            f"/collections/{COLLECTION}/points/search",
            {
                "vector": vector,
                "limit": limit,
                "score_threshold": score_threshold,
                "filter": {"must": [{"key": "class_id", "match": {"value": int(class_id)}}]},
                "with_payload": True,
                "with_vector": False,
            },
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, list):
            raise VectorUnavailable("向量检索响应无法解析")
        hits = []
        for item in result:
            if not isinstance(item, dict):
                continue
            hits.append({
                "id": int(item["id"]),
                "score": float(item.get("score") or 0),
                "payload": item.get("payload") if isinstance(item.get("payload"), dict) else {},
            })
        return hits

    def _get_collection(self) -> dict | None:
        try:
            body = self._request("GET", f"/collections/{COLLECTION}", None)
        except VectorUnavailable as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise
        return body

    def _request(self, method: str, path: str, payload: dict | None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            err = VectorUnavailable(f"Qdrant {exc.code}")
            err.status = exc.code  # type: ignore[attr-defined]
            if exc.code not in (404,):
                print(f"[QDRANT] {method} {path} -> {exc.code} {detail[:180]}")
            raise err from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise VectorUnavailable("Qdrant 不可用") from exc


def _collection_dim(body: dict) -> int:
    try:
        vectors = body["result"]["config"]["params"]["vectors"]
    except (KeyError, TypeError) as exc:
        raise VectorUnavailable("无法读取集合维度") from exc
    if isinstance(vectors, dict) and "size" in vectors:
        return int(vectors["size"])
    if isinstance(vectors, dict):
        for value in vectors.values():
            if isinstance(value, dict) and "size" in value:
                return int(value["size"])
    raise VectorUnavailable("无法读取集合维度")
