"""CampusClaw HTTP 服务：同源静态前端 + JSON API。

信任边界全部落在本进程：认证（会话）→ 授权（角色）→ 隔离（班级）逐请求强制执行。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import security
from .chunking import StrategyError
from .config import Config, TMP_DIR, UPLOAD_DIR, WEB_DIR
from .db import get_connection
from .mcpclient import McpError, connect_and_discover
from .openapi_spec import spec as openapi_spec
from .retrieval import (
    ASK_TOP,
    NOT_FOUND,
    RetrievalUnavailable,
    _clean_history as clean_ask_history,
    align_citations,
    ask as ask_question,
    build_chat_messages,
    chat_stream,
    hybrid_search,
    index_material,
    parse_strategy_request,
    reindex_material,
    search as search_knowledge,
)
from .skills import (
    parse_str_list,
    run_skill_tools,
    validate_tool_refs,
)

ALLOWED_EXT = {".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LoginRateLimiter:
    """进程内失败计数：key=(ip, username)，滑动窗口。线程安全。"""

    def __init__(self, max_failures: int, window_seconds: int):
        self.max = max_failures
        self.window = window_seconds
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: tuple[str, str]) -> bool:
        cutoff = time.time() - self.window
        with self._lock:
            times = [t for t in self._fails.get(key, []) if t > cutoff]
            self._fails[key] = times
            return len(times) >= self.max

    def record_failure(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(time.time())

    def reset(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._fails.pop(key, None)


# ---------------------------------------------------------------- multipart 解析

def parse_multipart(body: bytes, boundary: bytes) -> dict[str, dict]:
    """极简 multipart/form-data 解析。

    返回 {field_name: {"filename": str|None, "content_type": str, "data": bytes}}
    """
    delim = b"--" + boundary
    parts: dict[str, dict] = {}
    # 按边界切分；首块为前导（忽略），末块为 "--" 结束
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--\r\n", b"--", b"--\n", b"\r\n"):
            continue
        chunk = chunk.removeprefix(b"\r\n")
        if b"\r\n\r\n" not in chunk:
            continue
        header_blob, _, content = chunk.partition(b"\r\n\r\n")
        # 去掉尾部 CRLF
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers: dict[str, str] = {}
        for line in header_blob.decode("ascii", "replace").split("\r\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disp)
        if not name_match:
            continue
        fname_match = re.search(r'filename="([^"]*)"', disp)
        parts[name_match.group(1)] = {
            "filename": fname_match.group(1) if fname_match else None,
            "content_type": headers.get("content-type", "application/octet-stream"),
            "data": content,
        }
    return parts


# ---------------------------------------------------------------- HTTP handler

class Handler(BaseHTTPRequestHandler):
    server_version = "CampusClaw/1.0"
    config: Config = None  # type: ignore[assignment]
    limiter: LoginRateLimiter = None  # type: ignore[assignment]

    # ---- 基础工具 ----

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        extra_keys = set((extra or {}).keys())
        for k, v in SECURITY_HEADERS.items():
            if k not in extra_keys:
                self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, obj, extra: dict | None = None):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def send_empty(self, status: int = 204, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        extra_keys = set((extra or {}).keys())
        for k, v in SECURITY_HEADERS.items():
            if k not in extra_keys:
                self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def read_json(self) -> dict | None:
        raw = self.read_body()
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def log_message(self, fmt, *args):
        print(f"[{utcnow().strftime('%H:%M:%S')}] {self.address_string()} - {fmt % args}")

    # ---- 会话 ----

    def _bearer_token(self) -> str | None:
        header = self.headers.get("Authorization", "")
        scheme, _, rest = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        token = rest.strip()
        return token or None

    def current_user(self) -> sqlite3.Row | None:
        """校验 Bearer JWT，再按 token 哈希从库重读 user/role/class。"""
        token = self._bearer_token()
        if not token:
            return None
        payload = security.decode_access_token(token, self.config.session_secret)
        if payload is None:
            return None
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT u.id AS id, u.username AS username, u.role AS role,
                       u.class_id AS class_id, c.name AS class_name,
                       s.expires_at AS expires_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                JOIN classes c ON c.id = u.class_id
                WHERE s.token_hash = ?
                """,
                (security.hash_token(token),),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if row["expires_at"] <= utcnow().isoformat():
            return None
        if payload.get("user_id") != row["id"]:
            return None
        return row

    def require_user(self) -> sqlite3.Row | None:
        user = self.current_user()
        if user is None:
            self.send_json(401, {"error": "未登录或会话已过期"})
            return None
        return user

    # ---- 路由 ----

    def do_GET(self):  # noqa: N802
        try:
            return self._route_get()
        except sqlite3.Error as exc:
            self._db_failure(exc)

    def do_POST(self):  # noqa: N802
        try:
            return self._route_post()
        except sqlite3.Error as exc:
            self._db_failure(exc)

    def do_DELETE(self):  # noqa: N802
        try:
            return self._route_delete()
        except sqlite3.Error as exc:
            self._db_failure(exc)

    def do_HEAD(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._serve_static("/")
        else:
            self.send_empty(404)

    def _db_failure(self, exc: Exception):
        print(f"[DB ERROR] {exc!r} path={self.path}")
        if self.path.startswith("/api/"):
            self.send_json(503, {"error": "服务暂时不可用"})
        else:
            self.send_empty(503)

    def _route_get(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/health":
            self.send_json(200, {"status": "ok"})  # 不查库
            return

        if path == "/api/me":
            user = self.require_user()
            if user:
                self.send_json(200, {
                    "username": user["username"], "role": user["role"],
                    "class_id": user["class_id"], "class_name": user["class_name"],
                })
            return

        if path == "/api/search":
            user = self.require_user()
            if user:
                self._handle_search(user)
            return

        m = re.fullmatch(r"/api/materials(?:/(\d+)(?:/(file))?)?", path)
        if path == "/api/materials":
            user = self.require_user()
            if user:
                self._list_materials(user)
            return
        if m:
            user = self.require_user()
            if not user:
                return
            material_id = int(m.group(1))
            if m.group(2) == "file":
                self._download_file(user, material_id)
            else:
                self._material_detail(user, material_id)
            return

        # ---- 第 6 课：MCP 管理 / 技能 / 文档 ----
        if path == "/api/mcp/servers":
            user = self._require_teacher()
            if user:
                self._list_servers(user)
            return
        m = re.fullmatch(r"/api/mcp/servers/(\d+)/tools", path)
        if m:
            user = self._require_teacher()
            if user:
                self._list_server_tools(user, int(m.group(1)))
            return
        if path == "/api/mcp/audit":
            user = self._require_teacher()
            if user:
                self._list_audit(user)
            return
        if path == "/api/skills":
            user = self._require_teacher()
            if user:
                self._list_skills(user)
            return
        if path == "/api/apps":
            user = self.require_user()
            if user:
                self._list_apps(user)
            return
        if path == "/openapi.json":
            self.send_json(200, openapi_spec)
            return
        if path == "/docs":
            self._serve_swagger()
            return

        # 静态资源（白名单，禁止目录穿越，不含 /uploads）
        if path in ("/", "/index.html", "/static/style.css", "/static/app.js"):
            self._serve_static(path)
            return

        self.send_empty(404)

    def _route_post(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._handle_logout()
            return
        if path == "/api/materials":
            user = self.require_user()
            if not user:
                return
            if user["role"] != "teacher":
                self.send_json(403, {"error": "仅教师可上传材料"})
                return
            self._handle_upload(user)
            return
        if path == "/api/ask":
            user = self.require_user()
            if user:
                self._handle_ask(user)
            return
        if path == "/api/ask/stream":
            user = self.require_user()
            if user:
                self._handle_ask_stream(user)
            return

        # ---- 第 6 课：MCP 管理 / 技能 ----
        if path == "/api/mcp/servers":
            user = self._require_teacher()
            if user:
                self._register_server(user)
            return
        m = re.fullmatch(r"/api/mcp/servers/(\d+)/(connect|disconnect)", path)
        if m:
            user = self._require_teacher()
            if user:
                self._toggle_server(user, int(m.group(1)), m.group(2) == "connect")
            return
        if path == "/api/skills":
            user = self._require_teacher()
            if user:
                self._create_skill(user)
            return
        if path == "/api/apps":
            user = self._require_teacher()
            if user:
                self._create_app(user)
            return
        if path == "/api/skills/import":
            user = self._require_teacher()
            if user:
                self._import_skills(user)
            return

        reindex = re.fullmatch(r"/api/materials/(\d+)/reindex", path)
        if reindex:
            user = self.require_user()
            if not user:
                return
            if user["role"] != "teacher":
                self.send_json(403, {"error": "仅教师可重建索引"})
                return
            self._handle_reindex(user, int(reindex.group(1)))
            return

        self.send_empty(404)

    def _route_delete(self):
        path = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r"/api/mcp/servers/(\d+)", path)
        if m:
            user = self._require_teacher()
            if user:
                self._delete_server(user, int(m.group(1)))
            return
        m = re.fullmatch(r"/api/skills/(\d+)", path)
        if m:
            user = self._require_teacher()
            if user:
                self._delete_skill(user, int(m.group(1)))
            return
        m = re.fullmatch(r"/api/apps/(\d+)", path)
        if m:
            user = self._require_teacher()
            if user:
                self._delete_app(user, int(m.group(1)))
            return
        self.send_empty(404)

    # ---- 角色门槛 ----

    def _require_teacher(self):
        user = self.require_user()
        if user is None:
            return None
        if user["role"] != "teacher":
            self.send_json(403, {"error": "仅教师可执行此操作"})
            return None
        return user

    # ---- 登录 / 登出 ----

    def _handle_login(self):
        data = self.read_json()
        if not data or not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str):
            self.send_json(400, {"error": "请求格式错误"})
            return
        username = data["username"].strip()
        password = data["password"]
        if not username or not password:
            self.send_json(400, {"error": "请求格式错误"})
            return

        key = (self.client_ip(), username)
        if self.limiter.blocked(key):
            print(f"[AUTH] rate-limited user={username!r} ip={key[0]}")
            security.dummy_verify(password)  # 保持耗时近似
            self._login_failed()
            return

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id, username, password_hash, role, class_id FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None:
                security.dummy_verify(password)
                self.limiter.record_failure(key)
                print(f"[AUTH] unknown user={username!r}")
                self._login_failed()
                return
            if not security.verify_password(password, row["password_hash"]):
                self.limiter.record_failure(key)
                print(f"[AUTH] bad password user={username!r}")
                self._login_failed()
                return

            # 成功：签发 JWT，库存哈希以便登出即失效
            self.limiter.reset(key)
            token, exp_unix = security.issue_access_token(
                self.config.session_secret,
                user_id=row["id"],
                username=row["username"],
                role=row["role"],
                class_id=row["class_id"],
                ttl_seconds=self.config.session_ttl_seconds,
            )
            expires = datetime.fromtimestamp(exp_unix, timezone.utc)
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (security.hash_token(token), row["id"], utcnow().isoformat(), expires.isoformat()),
            )
            class_row = conn.execute("SELECT name FROM classes WHERE id = ?", (row["class_id"],)).fetchone()
        finally:
            conn.close()

        self.send_json(200, {
            "token": token,
            "token_type": "Bearer",
            "expires_in": self.config.session_ttl_seconds,
            "username": row["username"], "role": row["role"],
            "class_id": row["class_id"], "class_name": class_row["name"],
        })

    def _login_failed(self):
        self.send_json(401, {"error": "用户名或密码错误"})

    def _handle_logout(self):
        token = self._bearer_token()
        if token:
            conn = get_connection()
            try:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                             (security.hash_token(token),))
            finally:
                conn.close()
        self.send_empty(204)

    # ---- 材料：列表 / 详情 / 下载 ----

    def _list_materials(self, user: sqlite3.Row):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0].strip()
        conn = get_connection()
        try:
            if q:
                like = f"%{q}%"
                rows = conn.execute(
                    """
                    SELECT m.id, m.title, m.original_name, m.size_bytes, m.content_type,
                           m.created_at, u.username AS uploader
                    FROM materials m
                    JOIN users u ON u.id = m.uploader_id
                    WHERE m.class_id = ?
                      AND (m.title LIKE ?
                           OR EXISTS (SELECT 1 FROM knowledge_entries ke
                                      WHERE ke.material_id = m.id AND ke.content LIKE ?))
                    ORDER BY m.created_at DESC, m.id DESC
                    """,
                    (user["class_id"], like, like),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.id, m.title, m.original_name, m.size_bytes, m.content_type,
                           m.created_at, u.username AS uploader
                    FROM materials m
                    JOIN users u ON u.id = m.uploader_id
                    WHERE m.class_id = ?
                    ORDER BY m.created_at DESC, m.id DESC
                    """,
                    (user["class_id"],),
                ).fetchall()
        finally:
            conn.close()
        self.send_json(200, [dict(r) for r in rows])

    def _fetch_owned(self, user: sqlite3.Row, material_id: int) -> sqlite3.Row | None:
        """按 id + 会话班级联合查询；查不到就是 404（跨班与不存在同形）。"""
        conn = get_connection()
        try:
            return conn.execute(
                """
                SELECT m.*, ke.content AS content, u.username AS uploader
                FROM materials m
                JOIN knowledge_entries ke ON ke.material_id = m.id
                JOIN users u ON u.id = m.uploader_id
                WHERE m.id = ? AND m.class_id = ?
                """,
                (material_id, user["class_id"]),
            ).fetchone()
        finally:
            conn.close()

    def _material_detail(self, user: sqlite3.Row, material_id: int):
        row = self._fetch_owned(user, material_id)
        if row is None:
            self.send_json(404, {"error": "未找到材料"})
            return
        self.send_json(200, {
            "id": row["id"], "title": row["title"],
            "original_name": row["original_name"], "size_bytes": row["size_bytes"],
            "content_type": row["content_type"], "created_at": row["created_at"],
            "uploader": row["uploader"], "content": row["content"],
        })

    def _download_file(self, user: sqlite3.Row, material_id: int):
        row = self._fetch_owned(user, material_id)
        if row is None:
            self.send_json(404, {"error": "未找到材料"})
            return
        file_path = UPLOAD_DIR / row["stored_name"]
        if not file_path.is_file():
            print(f"[ERROR] missing file on disk: {file_path}")
            self.send_json(500, {"error": "文件不可用"})
            return
        data = file_path.read_bytes()
        encoded_name = urllib.parse.quote(row["original_name"])
        self._send(200, data, row["content_type"], {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Cache-Control": "no-store",
        })

    # ---- 上传 ----

    def _handle_upload(self, user: sqlite3.Row):
        declared = int(self.headers.get("Content-Length") or 0)
        if declared <= 0:
            self.send_json(400, {"error": "空请求"})
            return
        # 必须先把请求体读完/排空再响应，否则连接复用时会截断后续请求
        if declared > self.config.max_upload_bytes:
            # 分块排空但不落盘、不驻留内存
            remain = declared
            while remain > 0:
                chunk = self.rfile.read(min(remain, 64 * 1024))
                if not chunk:
                    break
                remain -= len(chunk)
            self.send_json(413, {"error": f"文件超过 {self.config.max_upload_bytes // 1024 // 1024} MiB 上限"})
            return

        ctype = self.headers.get("Content-Type", "")
        mb = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        if not mb:
            self.read_body()
            self.send_json(400, {"error": "请求格式错误"})
            return
        boundary = (mb.group(1) or mb.group(2)).strip().encode("ascii")
        body = self.read_body()
        if len(body) > self.config.max_upload_bytes:
            self.send_json(413, {"error": "文件超过大小上限"})
            return

        parts = parse_multipart(body, boundary)
        part = parts.get("file")
        if not part or not part.get("filename"):
            self.send_json(400, {"error": "缺少 file 字段"})
            return

        original_name = os.path.basename(part["filename"]).replace("\x00", "")
        ext = Path(original_name).suffix.lower()
        if ext not in ALLOWED_EXT:
            self.send_json(415, {"error": "仅支持 .txt / .md 文件"})
            return

        raw = part["data"]
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.send_json(422, {"error": "文件内容必须为 UTF-8 文本"})
            return

        try:
            strategy, strategy_options = parse_strategy_request(_form_fields(parts))
        except StrategyError as exc:
            self.send_json(422, {"error": str(exc)})
            return

        title = Path(original_name).stem[:200] or "未命名"
        tmp_path = TMP_DIR / security.new_session_token()[0]
        tmp_path.write_bytes(raw)

        conn = get_connection()
        try:
            now = utcnow().isoformat()
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """INSERT INTO materials
                   (class_id, uploader_id, title, original_name, stored_name,
                    size_bytes, content_type, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (user["class_id"], user["id"], title, original_name, "",
                 len(raw), ALLOWED_EXT[ext], now),
            )
            material_id = cur.lastrowid
            stored_name = f"{material_id}{ext}"
            conn.execute("UPDATE materials SET stored_name = ? WHERE id = ?",
                         (stored_name, material_id))
            entry_cur = conn.execute(
                """INSERT INTO knowledge_entries (material_id, class_id, content, created_at)
                   VALUES (?,?,?,?)""",
                (material_id, user["class_id"], content, now),
            )
            entry_id = entry_cur.lastrowid
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            tmp_path.unlink(missing_ok=True)
            raise
        finally:
            conn.close()

        # 事务提交后原子落盘；失败则回滚业务数据
        final_path = UPLOAD_DIR / stored_name
        try:
            os.replace(tmp_path, final_path)
        except OSError as exc:
            print(f"[UPLOAD ERROR] rename failed: {exc!r}")
            conn = get_connection()
            try:
                conn.execute("DELETE FROM knowledge_entries WHERE material_id = ?", (material_id,))
                conn.execute("DELETE FROM materials WHERE id = ?", (material_id,))
            finally:
                conn.close()
            tmp_path.unlink(missing_ok=True)
            self.send_json(500, {"error": "文件保存失败"})
            return

        stats = index_material(
            self.config,
            material_id=material_id,
            class_id=user["class_id"],
            entry_id=entry_id,
            body_text=content,
            strategy=strategy,
            options=strategy_options,
        )
        self.send_json(201, {
            "id": material_id, "title": title, "original_name": original_name,
            "size_bytes": len(raw), "content_type": ALLOWED_EXT[ext],
            "chunks_total": stats["chunks_total"],
            "chunks_failed": stats["chunks_failed"],
            "chunks_ready": stats["chunks_ready"],
        })

    def _handle_search(self, user: sqlite3.Row):
        # 查询串和请求头里的班级编号解析后丢弃，只使用会话班级
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        query.pop("class_id", None)
        query.pop("classId", None)
        _ = self.headers.get("X-Class-Id")
        q = query.get("q", [""])[0]
        mode = query.get("mode", ["hybrid"])[0]
        try:
            result = search_knowledge(self.config, int(user["class_id"]), q, mode)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        except RetrievalUnavailable:
            self.send_json(503, {"error": "服务暂时不可用"})
            return
        self.send_json(200, result)

    def _handle_ask(self, user: sqlite3.Row):
        data, question, messages = self._parse_ask_body()
        if data is None:
            self.send_json(400, {"error": "请求格式错误"})
            return
        # 命中技能则先带令牌调用其 MCP 工具（含审计），结果只作为数据
        try:
            _skill, tool_calls, tool_data = run_skill_tools(
                self.config, int(user["class_id"]), user, question)
        except Exception as exc:
            print(f"[SKILL] 编排失败（不阻断问答）: {exc!r}")
            tool_calls, tool_data = [], []
        try:
            system_prompt, app_name = self._pick_app(user, data)
            print(f"[ASK] app_id={data.get('app_id')} 应用={app_name or '默认'} "
                  f"提示词模板={'自定义' if system_prompt else '默认'}")
            result = ask_question(self.config, int(user["class_id"]), question,
                                  messages, tool_data=tool_data,
                                  system_prompt=system_prompt)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        except RetrievalUnavailable:
            self.send_json(503, {"error": "服务暂时不可用"})
            return
        result["tool_calls"] = tool_calls
        self.send_json(200, result)

    def _parse_ask_body(self) -> tuple[dict | None, str, list]:
        data = self.read_json()
        if not isinstance(data, dict):
            return None, "", []
        data.pop("class_id", None)
        data.pop("classId", None)
        _ = self.headers.get("X-Class-Id")
        question = data.get("question") if isinstance(data.get("question"), str) else ""
        if not question.strip() and isinstance(data.get("q"), str):
            question = data["q"]
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        return data, question, messages

    def _handle_ask_stream(self, user: sqlite3.Row):
        data, question, messages = self._parse_ask_body()
        if data is None or not question.strip():
            self.send_json(400, {"error": "请求格式错误"})
            return
        class_id = int(user["class_id"])
        try:
            skill, tool_calls, tool_data = run_skill_tools(
                self.config, class_id, user, question)
        except Exception as exc:
            print(f"[SKILL] 编排失败（不阻断问答）: {exc!r}")
            skill, tool_calls, tool_data = None, [], []
        system_prompt, app_name = self._pick_app(user, data)
        print(f"[ASK] app_id={data.get('app_id')} 应用={app_name or '默认'} "
              f"提示词模板={'自定义' if system_prompt else '默认'}")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        for k, v in SECURITY_HEADERS.items():
            if k != "Content-Security-Policy":
                self.send_header(k, v)
        self.end_headers()

        def sse(event: str, obj: dict):
            frame = (f"event: {event}\n"
                     f"data: {json.dumps(obj, ensure_ascii=False)}\n\n")
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()

        try:
            # meta 先行：技能与工具调用状态（含 denied）在这里可见
            sse("meta", {
                "skill": skill["name"] if skill else None,
                "tool_calls": tool_calls,
                "app": app_name,
            })
            hits = hybrid_search(self.config, class_id, question, limit=ASK_TOP)
        except RetrievalUnavailable:
            sse("error", {"error": "服务暂时不可用"})
            sse("done", {"answer": "", "citations": []})
            return

        if not hits:
            sse("delta", {"text": NOT_FOUND})
            sse("done", {"answer": NOT_FOUND, "citations": []})
            return
        if not self.config.chat_configured():
            sse("error", {"error": "对话网关未配置"})
            sse("done", {"answer": "", "citations": []})
            return

        history = clean_ask_history(messages, question)
        prompt_messages = build_chat_messages(question, hits, history, tool_data,
                                              system_prompt=system_prompt)
        buf: list[str] = []
        try:
            for piece in chat_stream(self.config, prompt_messages):
                buf.append(piece)
                sse("delta", {"text": piece})
        except RetrievalUnavailable as exc:
            sse("error", {"error": str(exc)})
            sse("done", {"answer": "".join(buf), "citations": []})
            return
        answer, citations = align_citations("".join(buf), hits)
        sse("done", {"answer": answer, "citations": citations})

    # ---- AI 应用（提示词模板） ----

    def _pick_app(self, user: sqlite3.Row, data: dict) -> tuple[str | None, str | None]:
        """按请求里的 app_id 取本班应用提示词模板；未选/不存在/他班返回 (None, None)。"""
        raw = data.get("app_id")
        try:
            app_id = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return None, None
        if app_id <= 0:
            return None, None
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT name, system_prompt FROM ai_apps WHERE id = ? AND class_id = ?",
                (app_id, user["class_id"]),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None, None
        return row["system_prompt"], row["name"]

    def _list_apps(self, user: sqlite3.Row):
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, name, description, system_prompt, created_at"
                " FROM ai_apps WHERE class_id = ? ORDER BY id",
                (user["class_id"],),
            ).fetchall()
        finally:
            conn.close()
        self.send_json(200, [dict(row) for row in rows])

    def _create_app(self, user: sqlite3.Row):
        data = self.read_json()
        if not isinstance(data, dict):
            self.send_json(400, {"error": "请求格式错误"})
            return
        name = data.get("name") if isinstance(data.get("name"), str) else ""
        name = name.strip()
        if not name or len(name) > 64:
            self.send_json(400, {"error": "应用名称必填（≤64 字符）"})
            return
        description = data.get("description") if isinstance(data.get("description"), str) else ""
        system_prompt = data.get("system_prompt") if isinstance(data.get("system_prompt"), str) else ""
        if not system_prompt.strip():
            self.send_json(400, {"error": "提示词模板（system_prompt）必填"})
            return
        conn = get_connection()
        try:
            dup = conn.execute(
                "SELECT 1 FROM ai_apps WHERE class_id = ? AND name = ?",
                (user["class_id"], name),
            ).fetchone()
            if dup:
                self.send_json(409, {"error": f"已存在同名应用：{name}"})
                return
            now = utcnow().isoformat()
            cur = conn.execute(
                """INSERT INTO ai_apps
                   (class_id, name, description, system_prompt, created_at)
                   VALUES (?,?,?,?,?)""",
                (user["class_id"], name, description.strip(),
                 system_prompt.strip(), now),
            )
            app_id = cur.lastrowid
        finally:
            conn.close()
        self.send_json(201, {
            "id": app_id, "name": name, "description": description.strip(),
            "system_prompt": system_prompt.strip(), "created_at": now,
        })

    def _delete_app(self, user: sqlite3.Row, app_id: int):
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM ai_apps WHERE id = ? AND class_id = ?",
                (app_id, user["class_id"]),
            ).fetchone()
            if row is None:
                self.send_json(404, {"error": "未找到应用"})
                return
            conn.execute("DELETE FROM ai_apps WHERE id = ?", (app_id,))
        finally:
            conn.close()
        self.send_empty(204)

    # ---- 技能批量导入 ----

    def _import_skills(self, user: sqlite3.Row):
        data = self.read_json()
        if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
            self.send_json(400, {"error": "请求体须为 {\"skills\": [...]}"})
            return
        items = data["skills"]
        if not items or len(items) > 50:
            self.send_json(400, {"error": "skills 须为 1~50 条"})
            return
        parsed = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                self.send_json(422, {"error": f"第 {index} 条不是对象"})
                return
            name = item.get("name") if isinstance(item.get("name"), str) else ""
            name = name.strip()
            if not name or len(name) > 64:
                self.send_json(422, {"error": f"第 {index} 条名称缺失或超长"})
                return
            try:
                tools = parse_str_list(item.get("tools"), "工具")
                keywords = parse_str_list(item.get("keywords"), "关键词")
                validate_tool_refs(int(user["class_id"]), tools)
            except ValueError as exc:
                self.send_json(422, {"error": f"第 {index} 条：{exc}"})
                return
            description = item.get("description") if isinstance(item.get("description"), str) else ""
            instructions = item.get("instructions") if isinstance(item.get("instructions"), str) else ""
            parsed.append((name, description.strip(), tools,
                           instructions.strip(), keywords))
        created = 0
        skipped: list[str] = []
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow().isoformat()
            for name, description, tools, instructions, keywords in parsed:
                dup = conn.execute(
                    "SELECT 1 FROM skills WHERE class_id = ? AND name = ?",
                    (user["class_id"], name),
                ).fetchone()
                if dup:
                    skipped.append(name)
                    continue
                conn.execute(
                    """INSERT INTO skills
                       (class_id, name, description, tools, instructions,
                        keywords, created_by, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (user["class_id"], name, description,
                     json.dumps(tools, ensure_ascii=False), instructions,
                     json.dumps(keywords, ensure_ascii=False),
                     user["id"], now, now),
                )
                created += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        self.send_json(201, {"created": created, "skipped": skipped})

    # ---- 第 6 课：MCP 服务器管理 ----

    def _list_servers(self, user: sqlite3.Row):
        """本班登记的服务器；响应绝不回显 auth_token。"""
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT s.id, s.name, s.base_url, s.status, s.last_error, s.created_at,
                       (SELECT COUNT(*) FROM mcp_tools t WHERE t.server_id = s.id) AS tool_count
                FROM mcp_servers s
                WHERE s.class_id = ?
                ORDER BY s.id
                """,
                (user["class_id"],),
            ).fetchall()
            tools = conn.execute(
                """SELECT t.server_id, t.name, t.description
                   FROM mcp_tools t
                   JOIN mcp_servers s ON s.id = t.server_id
                   WHERE t.class_id = ? ORDER BY t.id""",
                (user["class_id"],),
            ).fetchall()
        finally:
            conn.close()
        tools_by_server: dict[int, list] = {}
        for t in tools:
            tools_by_server.setdefault(t["server_id"], []).append(
                {"name": t["name"], "description": t["description"]})
        payload = []
        for row in rows:
            item = dict(row)
            item["tools"] = tools_by_server.get(row["id"], [])
            payload.append(item)
        self.send_json(200, payload)

    def _register_server(self, user: sqlite3.Row):
        data = self.read_json()
        if not isinstance(data, dict):
            self.send_json(400, {"error": "请求格式错误"})
            return
        name = data.get("name") if isinstance(data.get("name"), str) else ""
        base_url = data.get("base_url") if isinstance(data.get("base_url"), str) else ""
        token = data.get("auth_token") if isinstance(data.get("auth_token"), str) else ""
        name, base_url, token = name.strip(), base_url.strip().rstrip("/"), token.strip()
        if not name or len(name) > 64 or "@" in name or any(c.isspace() for c in name):
            self.send_json(400, {"error": "名称必填（≤64 字符，不含空格与 @）"})
            return
        if not base_url.startswith(("http://", "https://")):
            self.send_json(400, {"error": "Base URL 必须以 http:// 或 https:// 开头"})
            return
        conn = get_connection()
        try:
            dup = conn.execute(
                "SELECT 1 FROM mcp_servers WHERE class_id = ? AND name = ?",
                (user["class_id"], name),
            ).fetchone()
            if dup:
                self.send_json(409, {"error": f"已存在同名服务器：{name}"})
                return
            cur = conn.execute(
                """INSERT INTO mcp_servers
                   (class_id, name, base_url, auth_token, status, created_by, created_at)
                   VALUES (?,?,?,?, 'disconnected', ?, ?)""",
                (user["class_id"], name, base_url, token, user["id"], utcnow().isoformat()),
            )
            server_id = cur.lastrowid
        finally:
            conn.close()
        self.send_json(201, {
            "id": server_id, "name": name, "base_url": base_url,
            "status": "disconnected", "tool_count": 0, "tools": [],
        })

    def _fetch_server(self, user: sqlite3.Row, server_id: int) -> sqlite3.Row | None:
        """id + 会话班级联合查询；跨班与不存在同形 404。"""
        conn = get_connection()
        try:
            return conn.execute(
                "SELECT * FROM mcp_servers WHERE id = ? AND class_id = ?",
                (server_id, user["class_id"]),
            ).fetchone()
        finally:
            conn.close()

    def _toggle_server(self, user: sqlite3.Row, server_id: int, connect: bool):
        server = self._fetch_server(user, server_id)
        if server is None:
            self.send_json(404, {"error": "未找到 MCP 服务器"})
            return
        if connect:
            try:
                tools = connect_and_discover(server["base_url"], server["auth_token"])
            except McpError as exc:
                message = f"连接失败：{exc}"
                conn = get_connection()
                try:
                    conn.execute(
                        "UPDATE mcp_servers SET status = 'error', last_error = ? WHERE id = ?",
                        (str(exc), server_id),
                    )
                    conn.execute("DELETE FROM mcp_tools WHERE server_id = ?", (server_id,))
                finally:
                    conn.close()
                self.send_json(502, {"error": message})
                return
            conn = get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE mcp_servers SET status = 'connected', last_error = NULL WHERE id = ?",
                    (server_id,),
                )
                conn.execute("DELETE FROM mcp_tools WHERE server_id = ?", (server_id,))
                for tool in tools:
                    conn.execute(
                        """INSERT INTO mcp_tools
                           (server_id, class_id, tool_name, description, input_schema)
                           VALUES (?,?,?,?,?)""",
                        (server_id, user["class_id"], tool["name"],
                         tool["description"],
                         json.dumps(tool["input_schema"], ensure_ascii=False)),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
            print(f"[MCP] 已连接 {server['name']} ({server['base_url']})，发现 {len(tools)} 个工具")
            self.send_json(200, {
                "id": server_id, "name": server["name"],
                "base_url": server["base_url"], "status": "connected",
                "tool_count": len(tools), "tools": tools,
            })
        else:
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE mcp_servers SET status = 'disconnected', last_error = NULL WHERE id = ?",
                    (server_id,),
                )
                conn.execute("DELETE FROM mcp_tools WHERE server_id = ?", (server_id,))
            finally:
                conn.close()
            self.send_json(200, {
                "id": server_id, "name": server["name"],
                "base_url": server["base_url"], "status": "disconnected",
                "tool_count": 0, "tools": [],
            })

    def _delete_server(self, user: sqlite3.Row, server_id: int):
        server = self._fetch_server(user, server_id)
        if server is None:
            self.send_json(404, {"error": "未找到 MCP 服务器"})
            return
        conn = get_connection()
        try:
            conn.execute("DELETE FROM mcp_tools WHERE server_id = ?", (server_id,))
            conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        finally:
            conn.close()
        self.send_empty(204)

    def _list_server_tools(self, user: sqlite3.Row, server_id: int):
        server = self._fetch_server(user, server_id)
        if server is None:
            self.send_json(404, {"error": "未找到 MCP 服务器"})
            return
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT tool_name, description, input_schema FROM mcp_tools WHERE server_id = ? ORDER BY id",
                (server_id,),
            ).fetchall()
        finally:
            conn.close()
        payload = []
        for row in rows:
            try:
                schema = json.loads(row["input_schema"])
            except (json.JSONDecodeError, TypeError):
                schema = {}
            payload.append({"name": row["tool_name"],
                            "description": row["description"],
                            "input_schema": schema})
        self.send_json(200, payload)

    def _list_audit(self, user: sqlite3.Row):
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT id, server_name, tool_name, status, username, detail, created_at
                   FROM mcp_audit WHERE class_id = ? ORDER BY id DESC LIMIT 100""",
                (user["class_id"],),
            ).fetchall()
        finally:
            conn.close()
        payload = []
        for row in rows:
            item = dict(row)
            item["tool"] = f"{row['tool_name']}@{row['server_name']}"
            payload.append(item)
        self.send_json(200, payload)

    # ---- 第 6 课：技能 ----

    def _list_skills(self, user: sqlite3.Row):
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM skills WHERE class_id = ? ORDER BY id",
                (user["class_id"],),
            ).fetchall()
        finally:
            conn.close()
        payload = [_public_skill(row) for row in rows]
        self.send_json(200, payload)

    def _create_skill(self, user: sqlite3.Row):
        data = self.read_json()
        if not isinstance(data, dict):
            self.send_json(400, {"error": "请求格式错误"})
            return
        name = data.get("name") if isinstance(data.get("name"), str) else ""
        name = name.strip()
        if not name or len(name) > 64:
            self.send_json(400, {"error": "技能名称必填（≤64 字符）"})
            return
        try:
            tools = parse_str_list(data.get("tools"), "工具")
            keywords = parse_str_list(data.get("keywords"), "关键词")
            validate_tool_refs(int(user["class_id"]), tools)
        except ValueError as exc:
            self.send_json(422, {"error": str(exc)})
            return
        description = data.get("description") if isinstance(data.get("description"), str) else ""
        instructions = data.get("instructions") if isinstance(data.get("instructions"), str) else ""
        conn = get_connection()
        try:
            dup = conn.execute(
                "SELECT 1 FROM skills WHERE class_id = ? AND name = ?",
                (user["class_id"], name),
            ).fetchone()
            if dup:
                self.send_json(409, {"error": f"已存在同名技能：{name}"})
                return
            now = utcnow().isoformat()
            cur = conn.execute(
                """INSERT INTO skills
                   (class_id, name, description, tools, instructions,
                    keywords, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (user["class_id"], name, description.strip(),
                 json.dumps(tools, ensure_ascii=False), instructions.strip(),
                 json.dumps(keywords, ensure_ascii=False), user["id"], now, now),
            )
            skill_id = cur.lastrowid
        finally:
            conn.close()
        self.send_json(201, {
            "id": skill_id, "name": name, "description": description.strip(),
            "tools": tools, "instructions": instructions.strip(),
            "keywords": keywords, "created_at": now,
        })

    def _delete_skill(self, user: sqlite3.Row, skill_id: int):
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM skills WHERE id = ? AND class_id = ?",
                (skill_id, user["class_id"]),
            ).fetchone()
            if row is None:
                self.send_json(404, {"error": "未找到技能"})
                return
            conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        finally:
            conn.close()
        self.send_empty(204)

    def _serve_swagger(self):
        html = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>CampusClaw API 文档</title>"
            "<link rel='stylesheet' href='https://unpkg.com/swagger-ui-dist@5/swagger-ui.css'>"
            "</head><body><div id='swagger-ui'></div>"
            "<script src='https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js'></script>"
            "<script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui'});</script>"
            "</body></html>"
        ).encode("utf-8")
        # 仅此页面放宽 CSP 以加载 CDN 的 Swagger UI
        relaxed = dict(SECURITY_HEADERS)
        relaxed["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://unpkg.com; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com; img-src 'self' data: https://unpkg.com; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        self._send(200, html, "text/html; charset=utf-8", relaxed)

    def _handle_reindex(self, user: sqlite3.Row, material_id: int):
        raw_len = int(self.headers.get("Content-Length") or 0)
        data = self.read_json()
        if raw_len and not isinstance(data, dict):
            self.send_json(400, {"error": "请求格式错误"})
            return
        data = data or {}
        try:
            strategy, options = parse_strategy_request(data)
        except StrategyError as exc:
            self.send_json(422, {"error": str(exc)})
            return
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT ke.id AS entry_id, ke.content AS content
                FROM materials m
                JOIN knowledge_entries ke ON ke.material_id = m.id
                WHERE m.id = ? AND m.class_id = ?
                """,
                (material_id, user["class_id"]),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            self.send_json(404, {"error": "未找到材料"})
            return
        try:
            stats = reindex_material(
                self.config,
                material_id=material_id,
                class_id=int(user["class_id"]),
                entry_id=row["entry_id"],
                body_text=row["content"],
                strategy=strategy,
                options=options,
            )
        except RetrievalUnavailable:
            self.send_json(503, {"error": "服务暂时不可用"})
            return
        self.send_json(200, {"id": material_id, "strategy": strategy, **stats})

    # ---- 静态 ----

    def _serve_static(self, path: str):
        mapping = {
            "/": (WEB_DIR / "index.html", "text/html; charset=utf-8"),
            "/index.html": (WEB_DIR / "index.html", "text/html; charset=utf-8"),
            "/static/style.css": (WEB_DIR / "style.css", "text/css; charset=utf-8"),
            "/static/app.js": (WEB_DIR / "app.js", "application/javascript; charset=utf-8"),
        }
        file_path, ctype = mapping[path]
        if not file_path.is_file():
            self.send_empty(404)
            return
        body = file_path.read_bytes()
        self._send(200, body, ctype, {"Cache-Control": "no-store"})

    def client_ip(self) -> str:
        return self.client_address[0]


def _form_fields(parts: dict[str, dict]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, part in parts.items():
        if name == "file" or part.get("filename"):
            continue
        try:
            fields[name] = part.get("data", b"").decode("utf-8").strip()
        except UnicodeDecodeError:
            fields[name] = ""
    return fields


def _public_skill(row: sqlite3.Row) -> dict:
    """技能行转响应体（tools/keywords 从 JSON 还原）。"""
    def _load(text, fallback):
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else fallback
        except (json.JSONDecodeError, TypeError):
            return fallback

    return {
        "id": row["id"], "name": row["name"],
        "description": row["description"],
        "tools": _load(row["tools"], []),
        "instructions": row["instructions"],
        "keywords": _load(row["keywords"], []),
        "created_at": row["created_at"],
    }


def build_server(config: Config) -> ThreadingHTTPServer:
    Handler.config = config
    Handler.limiter = LoginRateLimiter(config.login_max_failures, config.login_window_seconds)
    httpd = ThreadingHTTPServer((config.host, config.port), Handler)
    return httpd
