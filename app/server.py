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
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import security
from .config import Config, TMP_DIR, UPLOAD_DIR, WEB_DIR
from .db import get_connection

COOKIE_NAME = "cc_session"
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
        for k, v in SECURITY_HEADERS.items():
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
        for k, v in SECURITY_HEADERS.items():
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

    def _cookies(self) -> dict[str, str]:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return {k: morsel.value for k, morsel in jar.items()}

    def current_user(self) -> sqlite3.Row | None:
        """每请求从库重读 user/role/class；会话过期或不存在返回 None。"""
        token = self._cookies().get(COOKIE_NAME)
        if not token:
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

        self.send_empty(404)

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

            # 成功：建会话
            self.limiter.reset(key)
            token, token_hash = security.new_session_token()
            now = utcnow()
            expires = now + timedelta(seconds=self.config.session_ttl_seconds)
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (token_hash, row["id"], now.isoformat(), expires.isoformat()),
            )
            class_row = conn.execute("SELECT name FROM classes WHERE id = ?", (row["class_id"],)).fetchone()
        finally:
            conn.close()

        cookie = (
            f"{COOKIE_NAME}={token}; HttpOnly; Path=/; SameSite=Lax; "
            f"Max-Age={self.config.session_ttl_seconds}"
        )
        self.send_json(200, {
            "username": row["username"], "role": row["role"],
            "class_id": row["class_id"], "class_name": class_row["name"],
        }, {"Set-Cookie": cookie})

    def _login_failed(self):
        self.send_json(401, {"error": "用户名或密码错误"})

    def _handle_logout(self):
        token = self._cookies().get(COOKIE_NAME)
        if token:
            conn = get_connection()
            try:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                             (security.hash_token(token),))
            finally:
                conn.close()
        self.send_empty(204, {"Set-Cookie": f"{COOKIE_NAME}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0"})

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
            conn.execute(
                """INSERT INTO knowledge_entries (material_id, class_id, content, created_at)
                   VALUES (?,?,?,?)""",
                (material_id, user["class_id"], content, now),
            )
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

        self.send_json(201, {
            "id": material_id, "title": title, "original_name": original_name,
            "size_bytes": len(raw), "content_type": ALLOWED_EXT[ext],
        })

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


def build_server(config: Config) -> ThreadingHTTPServer:
    Handler.config = config
    Handler.limiter = LoginRateLimiter(config.login_max_failures, config.login_window_seconds)
    httpd = ThreadingHTTPServer((config.host, config.port), Handler)
    return httpd
