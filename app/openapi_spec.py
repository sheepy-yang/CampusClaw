"""OpenAPI 3 文档（含第 6 课 MCP 管理接口），由 /openapi.json 提供、/docs 用 Swagger UI 试调。

课程项目为纯标准库实现，无框架装饰器，故以此集中声明代替注解生成。
"""
from __future__ import annotations

JSON = {"application/json": {"schema": {"type": "object"}}}

BEARER = [{"bearerAuth": []}]

_ERROR = {
    "description": "错误",
    "content": {"application/json": {"schema": {
        "type": "object", "properties": {"error": {"type": "string"}}}}},
}


def _resp(desc: str, schema: dict | None = None) -> dict:
    content = {"application/json": {"schema": schema}} if schema else JSON
    return {"description": desc, "content": content}


def _body(props: dict, required: list[str] | None = None) -> dict:
    return {
        "required": True,
        "content": {"application/json": {"schema": {
            "type": "object",
            "properties": props,
            "required": required or [],
        }}},
    }


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props}


SERVER = _obj({
    "id": {"type": "integer"}, "name": {"type": "string"},
    "base_url": {"type": "string"}, "status": {"type": "string",
        "enum": ["connected", "disconnected", "error"]},
    "last_error": {"type": "string", "nullable": True},
    "tool_count": {"type": "integer"},
    "tools": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "description": {"type": "string"}})},
    "created_at": {"type": "string"},
})
TOOL = _obj({
    "name": {"type": "string"}, "description": {"type": "string"},
    "input_schema": {"type": "object"},
})
SKILL = _obj({
    "id": {"type": "integer"}, "name": {"type": "string"},
    "description": {"type": "string"},
    "tools": {"type": "array", "items": {"type": "string"}},
    "instructions": {"type": "string"},
    "keywords": {"type": "array", "items": {"type": "string"}},
    "created_at": {"type": "string"},
})
APP = _obj({
    "id": {"type": "integer"}, "name": {"type": "string"},
    "description": {"type": "string"},
    "system_prompt": {"type": "string"},
    "created_at": {"type": "string"},
})
AUDIT = _obj({
    "id": {"type": "integer"}, "tool": {"type": "string"},
    "server_name": {"type": "string"}, "status": {"type": "string",
        "enum": ["ok", "denied", "error"]},
    "username": {"type": "string"}, "detail": {"type": "string"},
    "created_at": {"type": "string"},
})


def _id_param() -> dict:
    return {"name": "id", "in": "path", "required": True,
            "schema": {"type": "integer"}}

spec: dict = {
    "openapi": "3.0.3",
    "info": {
        "title": "CampusClaw API",
        "version": "6.0",
        "description": (
            "CampusClaw 教研知识库（第 6 课：自建 MCP 服务器、工具发现、"
            "技能绑定与鉴权调用、审计）。除 /health、/docs、/openapi.json、"
            "登录外均需 Bearer 会话令牌。"
        ),
    },
    "components": {
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
    },
    "paths": {
        "/health": {"get": {"summary": "存活探针（不查库）",
                            "responses": {"200": _resp("ok", _obj({"status": {"type": "string"}}))}}},
        "/api/login": {"post": {
            "summary": "登录，签发 Bearer 会话令牌",
            "requestBody": _body({
                "username": {"type": "string"}, "password": {"type": "string"}},
                ["username", "password"]),
            "responses": {"200": _resp("令牌与会话信息"), "401": _ERROR}}},
        "/api/me": {"get": {"summary": "当前用户与班级", "security": BEARER,
                            "responses": {"200": _resp("用户信息"), "401": _ERROR}}},
        "/api/search": {"get": {
            "summary": "本班知识检索（keyword/vector/hybrid）",
            "security": BEARER,
            "parameters": [
                {"name": "q", "in": "query", "required": True,
                 "schema": {"type": "string"}},
                {"name": "mode", "in": "query", "schema": {
                    "type": "string", "enum": ["keyword", "vector", "hybrid"]}},
            ],
            "responses": {"200": _resp("命中切片"), "400": _ERROR, "401": _ERROR}}},
        "/api/ask": {"post": {
            "summary": "知识问答（非流式；响应含 skill 与 tool_calls meta）",
            "security": BEARER,
            "requestBody": _body({
                "question": {"type": "string"},
                "messages": {"type": "array", "items": {"type": "object"}},
                "app_id": {"type": "integer", "description":
                    "可选：AI 应用编号，其提示词模板将作为 system 消息传给对话网关"}},
                ["question"]),
            "responses": {"200": _resp("回答与引用"), "400": _ERROR, "401": _ERROR,
                          "503": _ERROR}}},
        "/api/ask/stream": {"post": {
            "summary": "知识问答（SSE 流式：meta/delta/done/error 事件，meta 可见应用、技能与工具调用状态）",
            "security": BEARER,
            "requestBody": _body({
                "question": {"type": "string"},
                "messages": {"type": "array", "items": {"type": "object"}},
                "app_id": {"type": "integer", "description":
                    "可选：AI 应用编号，其提示词模板将作为 system 消息传给对话网关"}},
                ["question"]),
            "responses": {"200": {
                "description": "text/event-stream",
                "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                "400": _ERROR, "401": _ERROR}}},
        "/api/mcp/servers": {
            "get": {"summary": "本班已登记 MCP 服务器（响应不含令牌）",
                    "security": BEARER,
                    "responses": {"200": _resp("服务器数组",
                                               {"type": "array", "items": SERVER}),
                                  "401": _ERROR, "403": _ERROR}},
            "post": {"summary": "登记 MCP 服务器（令牌只存服务端）",
                     "security": BEARER,
                     "requestBody": _body({
                         "name": {"type": "string"},
                         "base_url": {"type": "string"},
                         "auth_token": {"type": "string"}},
                         ["name", "base_url"]),
                     "responses": {"201": _resp("已登记", SERVER), "400": _ERROR,
                                   "403": _ERROR, "409": _ERROR}}},
        "/api/mcp/servers/{id}/connect": {"post": {
            "summary": "连接：握手 initialize + tools/list，缓存工具清单",
            "security": BEARER,
            "parameters": [_id_param()],
            "responses": {"200": _resp("已连接", SERVER), "404": _ERROR,
                          "403": _ERROR, "502": _ERROR}}},
        "/api/mcp/servers/{id}/disconnect": {"post": {
            "summary": "断开：清空工具缓存，此后该服务器工具不可再调用",
            "security": BEARER,
            "parameters": [_id_param()],
            "responses": {"200": _resp("已断开", SERVER), "404": _ERROR,
                          "403": _ERROR}}},
        "/api/mcp/servers/{id}/tools": {"get": {
            "summary": "已缓存的工具清单（未连接为空数组）",
            "security": BEARER,
            "parameters": [_id_param()],
            "responses": {"200": _resp("工具数组",
                                       {"type": "array", "items": TOOL}),
                          "404": _ERROR, "403": _ERROR}}},
        "/api/mcp/servers/{id}": {"delete": {
            "summary": "删除登记（连带清空工具缓存）",
            "security": BEARER,
            "parameters": [_id_param()],
            "responses": {"204": {"description": "已删除"}, "404": _ERROR,
                          "403": _ERROR}}},
        "/api/mcp/audit": {"get": {
            "summary": "本班 MCP 工具调用审计（tool@server、状态、用户、时间）",
            "security": BEARER,
            "responses": {"200": _resp("审计数组",
                                       {"type": "array", "items": AUDIT}),
                          "403": _ERROR}}},
        "/api/skills": {
            "get": {"summary": "本班技能清单", "security": BEARER,
                    "responses": {"200": _resp("技能数组",
                                               {"type": "array", "items": SKILL}),
                                  "403": _ERROR}},
            "post": {"summary": (
                "创建技能；tools 支持 knowledge_search 与 工具名@服务器名，"
                "绑定的 MCP 工具必须属于本班、服务器已连接且已发现，否则 422"),
                "security": BEARER,
                "requestBody": _body({
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "instructions": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}}},
                    ["name", "tools"]),
                "responses": {"201": _resp("已创建", SKILL), "409": _ERROR,
                              "422": _ERROR, "403": _ERROR}}},
        "/api/skills/{id}": {"delete": {
            "summary": "删除技能", "security": BEARER,
            "parameters": [_id_param()],
            "responses": {"204": {"description": "已删除"}, "404": _ERROR,
                          "403": _ERROR}}},
        "/api/skills/import": {"post": {
            "summary": ("批量导入技能（上传技能 JSON）；逐条校验工具引用，"
                        "同名技能跳过，其余原子写入"),
            "security": BEARER,
            "requestBody": _body({
                "skills": {"type": "array", "items": _obj({
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "instructions": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}}})}},
                ["skills"]),
            "responses": {"201": _resp("创建数与跳过名单", _obj({
                "created": {"type": "integer"},
                "skipped": {"type": "array", "items": {"type": "string"}}})),
                "400": _ERROR, "422": _ERROR, "403": _ERROR}}},
        "/api/apps": {
            "get": {"summary": "本班 AI 应用清单（含提示词模板）", "security": BEARER,
                    "responses": {"200": _resp("应用数组",
                                               {"type": "array", "items": APP}),
                                  "401": _ERROR}},
            "post": {"summary": "创建 AI 应用（提示词模板必填）", "security": BEARER,
                     "requestBody": _body({
                         "name": {"type": "string"},
                         "description": {"type": "string"},
                         "system_prompt": {"type": "string"}},
                         ["name", "system_prompt"]),
                     "responses": {"201": _resp("已创建", APP), "400": _ERROR,
                                   "403": _ERROR, "409": _ERROR}}},
        "/api/apps/{id}": {"delete": {
            "summary": "删除 AI 应用（内置应用也可删除，重建库时自动补回）",
            "security": BEARER,
            "parameters": [_id_param()],
            "responses": {"204": {"description": "已删除"}, "404": _ERROR,
                          "403": _ERROR}}},
    },
}
