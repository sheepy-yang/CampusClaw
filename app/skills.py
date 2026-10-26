"""技能：登记校验、触发匹配、带令牌调用 MCP 工具、写审计。

工具引用规则：knowledge_search 或 工具名@服务器名（服务器为本班已登记且已连接，
工具必须来自连接后的发现缓存）。技能只提供数据：工具结果拼进用户消息，不当系统指令。
"""
from __future__ import annotations

import json
import re
import sqlite3

from . import mcpclient
from .mcpclient import McpAuthRejected, McpError

TOOL_REF_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_.\-]{0,63})@(\S{1,64})$")
RESULT_DETAIL_LIMIT = 160
RESULT_DATA_LIMIT = 2000

STATUS_OK = "ok"
STATUS_DENIED = "denied"
STATUS_ERROR = "error"


# ---------------------------------------------------------------- 登记校验

def parse_str_list(value, field: str) -> list[str]:
    """工具/关键词字段：接受 JSON 数组或逗号分隔字符串。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        items = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"{field} 必须是字符串数组")
            text = item.strip()
            if text:
                items.append(text)
        return items
    raise ValueError(f"{field} 必须是字符串数组")


def validate_tool_refs(class_id: int, tools: list[str]) -> None:
    """保存技能前校验工具引用。不满足则 ValueError（中文可读）。"""
    if not tools:
        raise ValueError("技能至少要绑定一个工具")
    seen: set[str] = set()
    for ref in tools:
        if ref == "knowledge_search":
            continue
        if ref in seen:
            raise ValueError(f"重复的工具引用：{ref}")
        seen.add(ref)
        match = TOOL_REF_RE.match(ref)
        if not match:
            raise ValueError(
                f"工具引用格式错误：{ref}（应为 工具名@服务器名，"
                "工具名以字母开头，仅含字母数字下划线点连字符）"
            )
        tool_name, server_name = match.group(1), match.group(2)
        conn = _conn()
        try:
            server = conn.execute(
                "SELECT id, status FROM mcp_servers WHERE class_id = ? AND name = ?",
                (class_id, server_name),
            ).fetchone()
            if server is None:
                raise ValueError(f"未登记的 MCP 服务器：{server_name}")
            if server["status"] != "connected":
                raise ValueError(f"MCP 服务器未连接：{server_name}（请先连接）")
            known = conn.execute(
                "SELECT 1 FROM mcp_tools WHERE server_id = ? AND tool_name = ?",
                (server["id"], tool_name),
            ).fetchone()
            if known is None:
                raise ValueError(
                    f"服务器 {server_name} 的工具清单中没有 {tool_name}"
                    "（请先重新连接完成发现）"
                )
        finally:
            conn.close()


# ---------------------------------------------------------------- 触发匹配

def match_skill(class_id: int, question: str) -> sqlite3.Row | None:
    """问题命中任一关键词即触发；关键词为空的技能不自动触发。"""
    lowered = (question or "").casefold()
    if not lowered:
        return None
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM skills WHERE class_id = ? ORDER BY id",
            (class_id,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            keywords = json.loads(row["keywords"])
        except (json.JSONDecodeError, TypeError):
            continue
        for keyword in keywords if isinstance(keywords, list) else []:
            if isinstance(keyword, str) and keyword and keyword.casefold() in lowered:
                return row
    return None


# ---------------------------------------------------------------- 工具调用编排

def run_skill_tools(config, class_id: int, user: sqlite3.Row,
                    question: str) -> tuple[sqlite3.Row | None, list[dict], list[str]]:
    """命中技能则逐个调用其 MCP 工具（带令牌），写审计。

    返回 (技能行|None, tool_calls meta, 拼进用户消息的数据块)。
    单个工具失败只影响自身状态，不阻断问答主路径。
    """
    skill = match_skill(class_id, question)
    if skill is None:
        return None, [], []
    try:
        refs = json.loads(skill["tools"])
    except (json.JSONDecodeError, TypeError):
        refs = []
    if not isinstance(refs, list):
        refs = []

    tool_calls: list[dict] = []
    tool_data: list[str] = []
    for ref in refs:
        if ref == "knowledge_search":
            continue
        match = TOOL_REF_RE.match(str(ref))
        if not match:
            continue
        tool_name, server_name = match.group(1), match.group(2)
        status, detail, text = _invoke(class_id, user, question,
                                       tool_name, server_name)
        tool_calls.append({
            "tool": tool_name, "server": server_name,
            "status": status, "detail": detail,
        })
        if text:
            tool_data.append(f"[{tool_name}@{server_name}] {text[:RESULT_DATA_LIMIT]}")
    return skill, tool_calls, tool_data


def _invoke(class_id: int, user: sqlite3.Row, question: str,
            tool_name: str, server_name: str) -> tuple[str, str, str | None]:
    """调用单个远端工具并记审计。返回 (status, detail, 工具文本|None)。"""
    conn = _conn()
    try:
        server = conn.execute(
            "SELECT * FROM mcp_servers WHERE class_id = ? AND name = ?",
            (class_id, server_name),
        ).fetchone()
    finally:
        conn.close()

    if server is None or server["status"] != "connected":
        status, detail, text = STATUS_DENIED, "服务器未连接或不存在", None
    else:
        try:
            text = mcpclient.call_tool(
                server["base_url"], server["auth_token"],
                tool_name, {"query": question},
            )
            status, detail = STATUS_OK, text[:RESULT_DETAIL_LIMIT]
        except McpAuthRejected as exc:
            status, detail, text = STATUS_DENIED, str(exc), None
        except McpError as exc:
            status, detail, text = STATUS_ERROR, str(exc), None
        except Exception as exc:  # 兜底：绝不让工具失败打断问答
            status, detail, text = STATUS_ERROR, f"工具调用异常：{exc}", None

    record_audit(class_id, server_name, tool_name, user, status, detail)
    return status, detail, text


def record_audit(class_id: int, server_name: str, tool_name: str,
                 user: sqlite3.Row, status: str, detail: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO mcp_audit
               (class_id, server_name, tool_name, user_id, username, status, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (class_id, server_name, tool_name, user["id"], user["username"],
             status, (detail or "")[:500], _utcnow()),
        )
    finally:
        conn.close()


def _conn() -> sqlite3.Connection:
    from .db import get_connection
    return get_connection()


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
