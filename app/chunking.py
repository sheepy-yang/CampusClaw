"""材料切分：auto / custom / hierarchy。只读正文，不改写原文。"""
from __future__ import annotations

import re
from dataclasses import dataclass

AUTO_MAX = 800
AUTO_OVERLAP = 80
# 空行、换行、句号，优先级从高到低
AUTO_SEPARATORS = ("\n\n", "\n", "。")

_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.+?)\s*$", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")

_SEP_NAMES = {
    "换行": "\n",
    "空行": "\n\n",
    "句号": "。",
    "newline": "\n",
    "blank": "\n\n",
    "period": "。",
    "\\n": "\n",
    "\\n\\n": "\n\n",
    "\n": "\n",
    "\n\n": "\n\n",
    "。": "。",
}


class StrategyError(ValueError):
    """切分参数不合法，调用方返回 422。"""


@dataclass(frozen=True)
class Chunk:
    text: str
    index: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class StrategyOptions:
    max_len: int = AUTO_MAX
    overlap: int = AUTO_OVERLAP
    separators: tuple[str, ...] = AUTO_SEPARATORS
    # custom 预处理后的文本；字符区间相对此文本，不是原文件下标
    source_text: str = ""


def parse_strategy_fields(fields: dict[str, str]) -> tuple[str, dict]:
    """解析上传 / 重建的策略字段。auto 忽略长度与预处理参数。"""
    strategy = (fields.get("strategy") or "auto").strip().lower()
    if strategy not in ("auto", "custom", "hierarchy"):
        raise StrategyError("不支持的切分策略")
    if strategy != "custom":
        return strategy, {}

    max_len = _parse_max_len(fields.get("max_len"))
    overlap = _parse_overlap(fields.get("overlap"))
    sep_raw = (fields.get("sep") or fields.get("separator") or "").strip()
    if sep_raw not in _SEP_NAMES:
        raise StrategyError("sep 须为换行、空行或句号")
    return "custom", {
        "max_len": max_len,
        "overlap": overlap,
        "sep": _SEP_NAMES[sep_raw],
        "remove_urls": _truthy(fields.get("remove_urls") or fields.get("strip_urls")),
        "collapse_ws": _truthy(
            fields.get("collapse_ws")
            or fields.get("collapse_whitespace")
            or fields.get("fold_whitespace")
        ),
    }


def split_text(body_text: str, strategy: str, options: dict | None = None) -> tuple[str, list[Chunk]]:
    """返回 (待切分文本, 切片)。custom 预处理时待切分文本不是原文。"""
    options = options or {}
    if strategy == "hierarchy":
        return body_text, _chunk_hierarchy(body_text)
    if strategy == "custom":
        source = _preprocess(
            body_text,
            remove_urls=bool(options.get("remove_urls")),
            collapse_ws=bool(options.get("collapse_ws")),
        )
        chunks = _window_chunks(
            source,
            max_len=int(options["max_len"]),
            overlap=_overlap_chars(int(options["max_len"]), float(options["overlap"])),
            separators=(str(options["sep"]),),
        )
        return source, chunks
    return body_text, _window_chunks(
        body_text, max_len=AUTO_MAX, overlap=AUTO_OVERLAP, separators=AUTO_SEPARATORS
    )


def _window_chunks(
    text: str, *, max_len: int, overlap: int, separators: tuple[str, ...]
) -> list[Chunk]:
    if not text:
        return []
    if overlap < 0 or overlap >= max_len:
        overlap = max(0, max_len - 1)
    n = len(text)
    chunks: list[Chunk] = []
    start = 0
    while start < n:
        hard = min(start + max_len, n)
        if hard >= n:
            end = n
        else:
            window = text[start:hard]
            cut = _prefer_break(window, overlap, separators)
            end = start + (cut if cut is not None else len(window))
        if end <= start:
            end = min(start + max_len, n)
            if end <= start:
                break
        chunks.append(Chunk(text[start:end], len(chunks), start, end))
        if end >= n:
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
    return chunks


def _prefer_break(window: str, overlap: int, separators: tuple[str, ...]) -> int | None:
    """在窗口内找优先分隔符，切点必须越过重叠长度，否则返回 None 表示强制截断。"""
    need = overlap + 1
    if need >= len(window):
        return None
    for sep in separators:
        pos = window.rfind(sep)
        while pos != -1:
            cut = pos + len(sep)
            if need <= cut <= len(window):
                return cut
            pos = window.rfind(sep, 0, pos)
    return None


def _chunk_hierarchy(text: str) -> list[Chunk]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return _window_chunks(
            text, max_len=AUTO_MAX, overlap=AUTO_OVERLAP, separators=AUTO_SEPARATORS
        )
    spans: list[tuple[int, int]] = []
    if matches[0].start() > 0:
        spans.append((0, matches[0].start()))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        spans.append((match.start(), end))
    chunks: list[Chunk] = []
    for sec_start, sec_end in spans:
        section = text[sec_start:sec_end]
        if not section.strip():
            continue
        for part in _window_chunks(
            section, max_len=AUTO_MAX, overlap=AUTO_OVERLAP, separators=AUTO_SEPARATORS
        ):
            chunks.append(Chunk(
                part.text,
                len(chunks),
                sec_start + part.char_start,
                sec_start + part.char_end,
            ))
    return chunks


def _preprocess(text: str, *, remove_urls: bool, collapse_ws: bool) -> str:
    if remove_urls:
        text = _URL_RE.sub("", text)
        text = _EMAIL_RE.sub("", text)
    if collapse_ws:
        text = re.sub(r"\s+", " ", text)
    return text


def _parse_max_len(raw: str | None) -> int:
    if raw is None or str(raw).strip() == "":
        raise StrategyError("custom 策略需要 max_len（100–2000）")
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise StrategyError("max_len 须为整数") from exc
    if value < 100 or value > 2000:
        raise StrategyError("max_len 须在 100–2000")
    return value


def _parse_overlap(raw: str | None) -> float:
    """返回 0–0.5 的比例。缺省为 0。'60%'、60、0.6 均视为越界。"""
    if raw is None or str(raw).strip() == "":
        return 0.0
    text = str(raw).strip()
    try:
        if text.endswith("%"):
            ratio = float(text[:-1].strip()) / 100.0
        else:
            value = float(text)
            ratio = value / 100.0 if value > 1 else value
    except ValueError as exc:
        raise StrategyError("overlap 须为 0%–50%") from exc
    if ratio < 0 or ratio > 0.5 + 1e-9:
        raise StrategyError("overlap 须在 0%–50%")
    return ratio


def _overlap_chars(max_len: int, ratio: float) -> int:
    chars = int(max_len * ratio)
    if chars >= max_len:
        return max_len - 1
    return max(0, chars)


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}
