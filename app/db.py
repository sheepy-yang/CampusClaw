"""SQLite 数据访问：连接、建表、目录初始化。"""
from __future__ import annotations

import sqlite3
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
"""


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
