"""SQLite 数据访问：连接、建表、目录初始化。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH, DATA_DIR, TMP_DIR, UPLOAD_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS classes (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('teacher', 'student')),
    class_id      INTEGER NOT NULL REFERENCES classes(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id      INTEGER NOT NULL REFERENCES classes(id),
    uploader_id   INTEGER NOT NULL REFERENCES users(id),
    title         TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_name   TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    content_type  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL UNIQUE REFERENCES materials(id),
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_materials_class ON materials(class_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id  INTEGER NOT NULL REFERENCES materials(id),
    class_id     INTEGER NOT NULL REFERENCES classes(id),
    chunk_index  INTEGER NOT NULL,
    chunk_text   TEXT NOT NULL,
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    embed_status TEXT NOT NULL DEFAULT 'failed'
                 CHECK (embed_status IN ('ready', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_class ON knowledge_chunks(class_id);
CREATE INDEX IF NOT EXISTS idx_chunks_material ON knowledge_chunks(material_id);

-- 第 6 课：本班登记的 MCP 服务器。Bearer 令牌只存这张表，任何接口不回显。
CREATE TABLE IF NOT EXISTS mcp_servers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    auth_token  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'disconnected'
                CHECK (status IN ('connected', 'disconnected', 'error')),
    last_error  TEXT,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL,
    UNIQUE (class_id, name)
);

-- 连接成功后缓存到的工具清单；断开即清空。
CREATE TABLE IF NOT EXISTS mcp_tools (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id    INTEGER NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    class_id     INTEGER NOT NULL REFERENCES classes(id),
    tool_name    TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    input_schema TEXT NOT NULL DEFAULT '{}',
    UNIQUE (server_id, tool_name)
);

-- MCP 工具调用审计：tool@server、状态、用户、时间。
CREATE TABLE IF NOT EXISTS mcp_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    server_name TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    username    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'denied', 'error')),
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_class ON mcp_audit(class_id);

-- 技能：tools 里除 knowledge_search 外支持 工具名@服务器名 的 MCP 引用。
CREATE TABLE IF NOT EXISTS skills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id     INTEGER NOT NULL REFERENCES classes(id),
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    tools        TEXT NOT NULL DEFAULT '["knowledge_search"]',
    instructions TEXT NOT NULL DEFAULT '',
    keywords     TEXT NOT NULL DEFAULT '[]',
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (class_id, name)
);

-- AI 应用：每个应用绑定一份提示词模板（system prompt），班级内名称唯一。
CREATE TABLE IF NOT EXISTS ai_apps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id      INTEGER NOT NULL REFERENCES classes(id),
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (class_id, name)
);
"""

# 内置 AI 应用：随建库为每个班级补齐，重复执行不重复插入。
DEFAULT_APPS = [
    {
        "name": "知识问答助手",
        "description": "默认应用：依据本班资料回答问题并标注引用",
        "system_prompt": (
            "你是本班教研资料助手。只根据用户消息里给出的资料切片回答，"
            "回答要简短，并在依据处用 [1]、[2] 这类序号标注，序号与资料编号一致。"
            "资料没有写到的内容不要编造。"
        ),
    },
    {
        "name": "公式讲解助手",
        "description": "先给结论再分步讲解推导过程",
        "system_prompt": (
            "你是公式讲解助手。先给出结论，再分步讲解推导过程，"
            "只依据用户消息里给出的资料切片，资料没写到的不要编造。"
        ),
    },
    {
        "name": "作业点评助手",
        "description": "指出作业中的错误并给出改进建议",
        "system_prompt": (
            "你是作业点评助手。指出答案中的错误并给出改进建议，语气鼓励，"
            "只依据用户消息里给出的资料切片，资料没写到的不要编造。"
        ),
    },
]


def seed_default_apps() -> None:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        for class_row in conn.execute("SELECT id FROM classes").fetchall():
            for app in DEFAULT_APPS:
                dup = conn.execute(
                    "SELECT 1 FROM ai_apps WHERE class_id = ? AND name = ?",
                    (class_row["id"], app["name"]),
                ).fetchone()
                if dup:
                    continue
                conn.execute(
                    """INSERT INTO ai_apps
                       (class_id, name, description, system_prompt, created_at)
                       VALUES (?,?,?,?,?)""",
                    (class_row["id"], app["name"], app["description"],
                     app["system_prompt"], now),
                )
    finally:
        conn.close()


def init_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    init_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def create_schema() -> None:
    init_dirs()
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    seed_default_apps()
