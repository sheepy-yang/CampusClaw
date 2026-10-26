"""主机侧 MCP 客户端：JSON-RPC 2.0 over HTTP，Bearer 令牌。

连接 = initialize 握手 + tools/list 发现；调用 = tools/call 取文本结果。
所有失败都收敛为 McpError 子类，消息可直接展示给教师，不阻断检索与问答主路径。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "campusclaw", "version": "1.0.0"}
CONNECT_TIMEOUT = 5   # 握手/发现
CALL_TIMEOUT = 8      # 工具调用


class McpError(Exception):
    """MCP 调用失败，message 可直接展示。"""


class McpAuthRejected(McpError):
    """远端 401：令牌缺失或不正确 → 主机侧记为 denied（越权/未授权）。"""


class McpUnavailable(McpError):
    """网络不可达 / 非 JSON 响应 / 远端 5xx → 记为 error。"""


def _rpc(base_url: str, token: str, method: str, params: dict | None,
         rpc_id: int = 1, timeout: int = CONNECT_TIMEOUT):
    payload: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        base_url.rstrip("/") + "/mcp",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise McpAuthRejected("远端返回 401：Bearer 令牌缺失或不正确") from exc
        raise McpUnavailable(f"远端返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise McpUnavailable(f"无法访问 {base_url}：{reason}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise McpUnavailable("远端响应不是 JSON") from exc
    if not isinstance(data, dict) or "result" not in data:
        err = data.get("error") if isinstance(data, dict) else data
        raise McpUnavailable(f"JSON-RPC 错误：{err}")
    return data["result"]


def initialize(base_url: str, token: str) -> dict:
    """握手。服务器信息异常视为协议错误。"""
    result = _rpc(base_url, token, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": CLIENT_INFO,
    })
    if not isinstance(result, dict) or "protocolVersion" not in result:
        raise McpUnavailable("initialize 响应缺少 protocolVersion")
    return result


def notify_initialized(base_url: str, token: str) -> None:
    """initialized 通知（无 id）。失败可忽略，不影响发现。"""
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/mcp",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            resp.read()
    except (urllib.error.URLError, OSError):
        pass  # 通知失败不阻断


def list_tools(base_url: str, token: str) -> list[dict]:
    """发现工具，统一成 {"name","description","input_schema"}。"""
    result = _rpc(base_url, token, "tools/list", {})
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        raise McpUnavailable("tools/list 响应缺少 tools 数组")
    normalized = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        name = tool["name"].strip()
        if not name:
            continue
        normalized.append({
            "name": name,
            "description": str(tool.get("description") or ""),
            "input_schema": tool.get("inputSchema") or {},
        })
    return normalized


def connect_and_discover(base_url: str, token: str) -> list[dict]:
    """完整连接流程：握手 → initialized 通知 → 发现。返回工具清单。"""
    initialize(base_url, token)
    notify_initialized(base_url, token)
    return list_tools(base_url, token)


def call_tool(base_url: str, token: str, tool_name: str,
              arguments: dict | None = None) -> str:
    """tools/call，提取并拼接 text 内容。空文本视为失败。"""
    result = _rpc(
        base_url, token, "tools/call",
        {"name": tool_name, "arguments": arguments or {}},
        timeout=CALL_TIMEOUT,
    )
    if not isinstance(result, dict):
        raise McpUnavailable("tools/call 响应格式错误")
    if result.get("isError"):
        raise McpUnavailable("工具执行返回 isError")
    pieces = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            pieces.append(str(item.get("text") or ""))
    text = "\n".join(piece for piece in pieces if piece.strip()).strip()
    if not text:
        raise McpUnavailable("工具返回空文本内容")
    return text
