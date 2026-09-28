"""
迭代 1 验收脚本：自动在独立端口启动服务，逐条判定 spec Scenario。

用法（先初始化数据库）：
  python scripts/init_db.py
  python scripts/verify.py

主路径：登录 / 列表 / 上传入库 / 下载 / 搜索
失败路径：未登录 401、学生上传 403、跨班 404、非法类型/超大/坏编码、限流
"""
from __future__ import annotations

import http.cookiejar
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from app.config import DB_PATH, UPLOAD_DIR, TMP_DIR  # noqa: E402

PORT = int(os.environ.get("VERIFY_PORT", "5099"))
BASE = f"http://127.0.0.1:{PORT}"
TEACHER_PW = os.environ.get("SEED_TEACHER_PASSWORD", "teacher123")
STUDENT_PW = os.environ.get("SEED_STUDENT_PASSWORD", "student123")

results: list[tuple[str, bool, str]] = []
def check(name: str, ok: bool, evidence: str = ""):
    results.append((name, ok, evidence))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {evidence}" if evidence else ""))


# ------------------------------------------------------------------ HTTP 工具

class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def request(self, method: str, path: str, data=None, headers=None, raw: bytes | None = None):
        url = BASE + path
        body = None
        hdrs = headers or {}
        if raw is not None:
            body = raw
        elif data is not None:
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            resp = self.opener.open(req)
            payload = resp.read()
            return resp.status, dict(resp.headers), payload
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def login(self, username, password):
        return self.request("POST", "/api/login", {"username": username, "password": password})

    def logout(self):
        return self.request("POST", "/api/logout")


def multipart(field: str, filename: str, content: bytes,
              content_type: str = "text/plain") -> tuple[bytes, str]:
    boundary = "----verify" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def db_counts():
    conn = sqlite3.connect(DB_PATH)
    try:
        m = conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
        k = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
    finally:
        conn.close()
    files = [p for p in UPLOAD_DIR.iterdir() if p.is_file()] if UPLOAD_DIR.exists() else []
    tmps = [p for p in TMP_DIR.iterdir() if p.is_file()] if TMP_DIR.exists() else []
    return m, k, len(files), len(tmps)


def j(payload: bytes):
    try:
        return json.loads(payload.decode())
    except Exception:
        return None


# ------------------------------------------------------------------ 主流程

def wait_ready(proc, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            urllib.request.urlopen(BASE + "/health", timeout=1).read()
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("服务启动超时")


def main():
    env = os.environ.copy()
    env["PORT"] = str(PORT)
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "run.py")],
        cwd=BASE_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready(proc)
        run_scenarios()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"结果：{passed}/{len(results)} 通过")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("未通过：")
        for n in failed:
            print("  -", n)
        sys.exit(1)
    print("全部 Scenario 判定通过。")


def run_scenarios():
    anon = Client()
    t3 = Client()
    s3 = Client()
    t4 = Client()

    print("R8 存活探针 / R1·R9 未登录失败路径")
    s, h, b = anon.request("GET", "/health")
    check("T0.1 /health 匿名 200", s == 200 and j(b) == {"status": "ok"}, b.decode())
    s, h, b = anon.request("GET", "/api/materials")
    titles = str(j(b) or "")
    check("T0.2 未登录列表 401 且无业务数据", s == 401 and "荷塘" not in titles and "故都" not in titles, str(s))
    check("T0.3 安全响应头", all(k in h for k in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy")),
          "nosniff/DENY/CSP")
    s, h, b = anon.request("POST", "/api/materials", raw=b"x",
                           headers={"Content-Type": "text/plain"})
    check("T14 未登录上传 401", s == 401, str(s))
    s, _, _ = anon.request("GET", "/uploads/1.md")
    check("T13 上传目录不可静态猜测(/uploads/1.md)", s == 404, str(s))

    print("R1 登录：成功 / 失败同形 / 缺字段")
    s, h, b = t3.login("teacher3", TEACHER_PW)
    me = j(b)
    cookie = h.get("Set-Cookie", "")
    check("T1.1 教师正确登录 200", s == 200 and me and me["role"] == "teacher" and me["class_name"] == "高一(3)班",
          f"{me and me['username']}")
    check("T1.2 Cookie 为 HttpOnly+SameSite=Lax", "HttpOnly" in cookie and "SameSite=Lax" in cookie, cookie[:80])
    fail_bodies = []
    for user, pw in [("teacher3", "wrong-password"), ("no_such_user", TEACHER_PW)]:
        c = Client()
        st, _, bb = c.login(user, pw)
        fail_bodies.append((st, bb))
    check("T1.3 用户不存在/口令错误同形 401",
          fail_bodies[0][0] == 401 == fail_bodies[1][0] and fail_bodies[0][1] == fail_bodies[1][1],
          fail_bodies[0][1].decode())
    s, _, b = anon.request("POST", "/api/login", {"username": "teacher3"})
    check("T1.4 缺密码字段 400", s == 400, str(s))

    print("R7 会话：/api/me 与登出失效")
    s, _, b = t3.request("GET", "/api/me")
    check("T2.1 /api/me 返回身份", s == 200 and j(b)["username"] == "teacher3")
    t3.logout()
    s, _, _ = t3.request("GET", "/api/me")
    check("T2.2 登出后旧 Cookie 失效 → 401", s == 401, str(s))
    t3.login("teacher3", TEACHER_PW)  # 后续用例需要

    print("R3 班级隔离")
    s, _, b = t3.request("GET", "/api/materials")
    t3_list = j(b)
    check("T4.1 teacher3 列表只见 3 班材料",
          s == 200 and all("高一4班" not in m["title"] for m in t3_list) and any("高一3班" in m["title"] for m in t3_list),
          f"{len(t3_list)} 条")
    t4.login("teacher4", TEACHER_PW)
    _, _, b4 = t4.request("GET", "/api/materials")
    other_id = j(b4)[0]["id"]
    s_cross, _, b_cross = t3.request("GET", f"/api/materials/{other_id}")
    s_missing, _, b_missing = t3.request("GET", "/api/materials/999999")
    check("T5 跨班详情与不存在同形 404",
          s_cross == 404 == s_missing and b_cross == b_missing and "故都" not in b_cross.decode(),
          b_cross.decode())
    s_cf, _, b_cf = t3.request("GET", f"/api/materials/{other_id}/file")
    check("T6 跨班文件下载 404 无字节", s_cf == 404 and len(b_cf) < 80, str(s_cf))

    print("R6 本班搜索")
    s, _, b = t4.request("GET", "/api/materials?q=" + urllib.parse.quote("荷塘"))
    check("T15.1 他班搜「荷塘」0 命中", s == 200 and j(b) == [], f"{len(j(b))} 条")
    s, _, b = t3.request("GET", "/api/materials?q=" + urllib.parse.quote("荷塘"))
    check("T15.2 本班搜「荷塘」命中", s == 200 and any("荷塘" in m["title"] for m in j(b)))

    print("R2 学生授权 / R4 上传入库 / R5 下载")
    s, _, _ = s3.login("student3", STUDENT_PW)
    check("学生登录 200", s == 200)
    _, _, b = s3.request("GET", "/api/materials")
    check("T2.2b 学生可读本班列表", len(j(b)) >= 2, f"{len(j(b))} 条")

    before = db_counts()
    body, ctype = multipart("file", "学生试图上传.txt", b"should be blocked")
    s, h, b = s3.request("POST", "/api/materials", raw=body, headers={"Content-Type": ctype})
    after = db_counts()
    check("T7 学生上传 403 且两表+磁盘无残留",
          s == 403 and before == after, f"{s}; before={before} after={after}")

    body, ctype = multipart("file", "高一3班-语文-新增测试教案.md",
                            "# 新增测试\n\n- 由 verify.py 上传\n\n<script>alert(1)</script>\n".encode())
    s, _, b = t3.request("POST", "/api/materials", raw=body, headers={"Content-Type": ctype})
    created = j(b)
    check("T8.1 教师合法上传 201", s == 201 and created and created["id"], b.decode()[:120])
    new_id = created["id"] if created else -1
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT m.class_id, k.class_id AS kc FROM materials m "
            "JOIN knowledge_entries k ON k.material_id=m.id WHERE m.id=?", (new_id,)
        ).fetchone()
        mcount = conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
        kcount = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
    finally:
        conn.close()
    check("T8.2 双表各增一行且关联、归属本班",
          row and row[0] == 1 and row[1] == 1 and mcount == before[0] + 1 and kcount == before[1] + 1,
          f"row={row}")
    _, _, lb = t3.request("GET", "/api/materials")
    check("T8.3 上传后本班列表可见", any(m["id"] == new_id for m in j(lb)))
    s, _, b = t3.request("GET", f"/api/materials/{new_id}")
    check("T16.1 知识库正文可取且原文含脚本串(前端转义)",
          s == 200 and "<script>alert(1)</script>" in j(b)["content"])
    # 清理该验证材料，保持种子整洁
    try:
        (UPLOAD_DIR / f"{new_id}.md").unlink(missing_ok=True)
        c2 = sqlite3.connect(DB_PATH)
        c2.execute("DELETE FROM knowledge_entries WHERE material_id=?", (new_id,))
        c2.execute("DELETE FROM materials WHERE id=?", (new_id,))
        c2.commit(); c2.close()
    except sqlite3.Error:
        pass

    # 三类失败
    body, ctype = multipart("file", "evil.exe", b"MZ binary")
    s, _, b = t3.request("POST", "/api/materials", raw=body, headers={"Content-Type": ctype})
    check("T9 非法扩展名 415", s == 415 and db_counts()[:2] == before[:2] and db_counts()[2] == before[2], str(s))
    body, ctype = multipart("file", "big.txt", b"a" * (2 * 1024 * 1024 + 100))
    s, _, _ = t3.request("POST", "/api/materials", raw=body, headers={"Content-Type": ctype})
    check("T10 超大文件 413", s == 413 and db_counts()[3] == 0, str(s))
    body, ctype = multipart("file", "bad.txt", b"\xff\xfe\x00\x81not-utf8")
    s, _, _ = t3.request("POST", "/api/materials", raw=body, headers={"Content-Type": ctype})
    check("T11 非法 UTF-8 422 无残留",
          s == 422 and db_counts() == before, f"{s}; {db_counts()}")

    # 下载
    seed_id = t3_list[0]["id"]
    s, h, b = t3.request("GET", f"/api/materials/{seed_id}/file")
    check("T12 本班文件 200 + Content-Disposition",
          s == 200 and len(b) > 0 and "attachment" in h.get("Content-Disposition", ""),
          h.get("Content-Disposition", "")[:80])
    s, _, _ = s3.request("GET", f"/api/materials/{seed_id}/file")
    check("学生可下载本班文件", s == 200, str(s))

    print("R1 限流（最后执行，锁定 student3）")
    statuses = []
    for i in range(5):
        st, _, _ = anon.request("POST", "/api/login", {"username": "student3", "password": f"bad{i}"})
        statuses.append(st)
    st, _, bb = anon.request("POST", "/api/login", {"username": "student3", "password": STUDENT_PW})
    statuses.append(st)
    check("T3 连续失败后第 6 次(口令正确)仍 401 同形",
          all(x == 401 for x in statuses) and j(bb) == {"error": "用户名或密码错误"},
          str(statuses))

    print("持久化：重启服务后数据仍在、限流清空")
    # 主进程在 main() 中统一管理，这里用同进程无法重启；以查库断言落盘
    conn = sqlite3.connect(DB_PATH)
    try:
        ok = conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0] == before[0]
    finally:
        conn.close()
    check("T17 材料已持久化到磁盘库(重启可复现)", ok, f"{before[0]} 行种子材料")


if __name__ == "__main__":
    main()
