#!/usr/bin/env python3
"""CampusClaw MCP 接口测试（课程第 6 课 MCP 集成作业）。

用途：
    对主应用（高中教研知识库 CampusClaw，Python 标准库 HTTP 服务）与自建
    MCP 服务器做端到端接口测试，覆盖：MCP 鉴权（无/错令牌 401）、工具
    initialize/tools/list/tools/call、服务器登记与连接（学生 403）、技能
    保存与工具引用校验（422）、流式问答中的工具调用与拒绝（SSE meta 帧）、
    调用审计（ok/denied）、断开连接后的行为与清理。全部用例纯标准库实现
    （urllib/json/html/datetime），单文件、无第三方依赖。

运行方式：
    python apitest/run_all.py            # 顺序执行全部用例并生成报告
    python apitest/run_all.py --help     # 查看用法

前置条件：
    1. 主应用已启动（默认 http://127.0.0.1:5000，可用环境变量 APITEST_BASE
       覆盖）；
    2. 自建 MCP 服务器已启动（默认 http://127.0.0.1:8090，可用环境变量
       APITEST_MCP_BASE 覆盖）；
    3. 仓库根目录 .env（或环境变量）提供 MCP_AUTH_TOKEN 与种子口令
       SEED_TEACHER_PASSWORD / SEED_STUDENT_PASSWORD，缺失时以退出码 2 报错；
    4. 测试账号 teacher3（教师，class 1）、student3（学生，class 1）已由
       种子脚本创建。

说明：
    测试报告写到 apitest/report.html，仅供本地查看，不进 Git。
"""

import datetime
import html
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

USAGE = """\
CampusClaw MCP 接口测试（课程第 6 课 MCP 集成作业）

用法：
    python apitest/run_all.py          # 顺序执行全部用例并生成 apitest/report.html
    python apitest/run_all.py --help   # 显示本帮助

环境变量（也可写在仓库根目录 .env，环境变量优先）：
    APITEST_BASE            主应用地址，默认 http://127.0.0.1:5000
    APITEST_MCP_BASE        自建 MCP 服务器地址，默认 http://127.0.0.1:8090
    MCP_AUTH_TOKEN          自建 MCP 服务器 Bearer 令牌（必需）
    SEED_TEACHER_PASSWORD   teacher3 口令（必需）
    SEED_STUDENT_PASSWORD   student3 口令（必需）

前置条件：主应用与 mcp-server 已启动；测试账号 teacher3/student3 已创建。
报告写到 apitest/report.html，不进 Git。
"""


def load_dotenv(path):
    """解析 KEY=VALUE 行；忽略 # 注释与空行，支持可选 export 前缀与引号值。"""
    result = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("export "):
            s = s[len("export "):].strip()
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            result[key] = val
    return result


_ENV = load_dotenv(REPO_ROOT / ".env")


def _cfg(name, default=None):
    return os.environ.get(name) or _ENV.get(name) or default


BASE = _cfg("APITEST_BASE", "http://127.0.0.1:5000")
MCP_BASE = _cfg("APITEST_MCP_BASE", "http://127.0.0.1:8090")
MCP_AUTH_TOKEN = _cfg("MCP_AUTH_TOKEN")
TEACHER_PW = _cfg("SEED_TEACHER_PASSWORD")
STUDENT_PW = _cfg("SEED_STUDENT_PASSWORD")

TEACHER = "teacher3"
STUDENT = "student3"


# ---------------------------------------------------------------- HTTP 帮助

def req(method, url, token=None, body=None, timeout=15):
    """发送 HTTP 请求，返回 (status, data)；data 尽量解析为 JSON，否则原文。"""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read()
        except Exception:
            raw = b""
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        return 0, f"连接失败: {e}"
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def parse_frame(text):
    """解析一帧 SSE（event:/data: 行），返回 (event, data) 或 None。"""
    event = None
    datas = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            datas.append(line[len("data:"):].strip())
    if event is None and not datas:
        return None
    raw = "\n".join(datas)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    return (event or "message", payload)


def read_sse(url, token, body, timeout=10, max_seconds=60.0):
    """POST 并读取 SSE 流，返回 (status, frames)。

    逐块读取、按 \\n\\n 分帧；拿到 done/error 帧即停止并关闭连接（此时
    body 已读到关键帧，不会挂起），或读到流结束/超时为止。
    """
    frames = []
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        resp = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, frames
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        print(f"  (SSE 连接失败: {e})")
        return 0, frames
    try:
        buf = b""
        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            try:
                chunk = resp.read1(4096)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            buf += chunk.replace(b"\r\n", b"\n")
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                frame = parse_frame(raw.decode("utf-8", "replace"))
                if frame:
                    frames.append(frame)
                    if frame[0] in ("done", "error"):
                        return resp.status, frames
        if buf.strip():
            frame = parse_frame(buf.decode("utf-8", "replace"))
            if frame:
                frames.append(frame)
    finally:
        resp.close()
    return resp.status, frames


def tool_names(tools):
    names = []
    for t in tools or []:
        names.append(t.get("name") if isinstance(t, dict) else t)
    return [n for n in names if n]


def tool_text(data):
    """从 tools/call 响应里取 result.content[0].text。"""
    try:
        return data["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return ""


def need(*checks):
    """checks 为 (是否满足, 不满足原因)；全部满足返回 None，否则第一个原因。"""
    for ok, reason in checks:
        if not ok:
            return reason
    return None


def truncate(text, limit=280):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------- 用例

def case_mcp_health(ctx):
    status, data = req("GET", MCP_BASE + "/health")
    if status != 200:
        return False, f"HTTP {status}，期望 200"
    if isinstance(data, dict) and data.get("status") == "ok":
        return True, "HTTP 200，status=ok"
    return False, f"HTTP 200 但响应异常: {truncate(data)}"


def case_mcp_no_token_401(ctx):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    status, _ = req("POST", MCP_BASE + "/mcp", body=body)
    return status == 401, f"HTTP {status}，期望 401（无 Authorization）"


def case_mcp_bad_token_401(ctx):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    status, _ = req("POST", MCP_BASE + "/mcp", token="wrong-token", body=body)
    return status == 401, f"HTTP {status}，期望 401（Bearer wrong-token）"


def case_mcp_tools_list(ctx):
    status, data = req("POST", MCP_BASE + "/mcp", token=MCP_AUTH_TOKEN,
                       body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    ok_init = (status == 200 and isinstance(data, dict)
               and isinstance(data.get("result"), dict)
               and "protocolVersion" in data["result"])
    status2, data2 = req("POST", MCP_BASE + "/mcp", token=MCP_AUTH_TOKEN,
                         body={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = []
    if status2 == 200 and isinstance(data2, dict) and isinstance(data2.get("result"), dict):
        tools = data2["result"].get("tools") or []
    names = tool_names(tools)
    ok_tools = "formula_hint" in names and "unit_convert" in names
    detail = (f"initialize HTTP {status}，protocolVersion={'存在' if ok_init else '缺失'}；"
              f"tools/list HTTP {status2}，tools={names}")
    return ok_init and ok_tools, detail


def case_mcp_tools_call(ctx):
    def call(name, args):
        body = {"jsonrpc": "2.0", "id": name, "method": "tools/call",
                "params": {"name": name, "arguments": args}}
        return req("POST", MCP_BASE + "/mcp", token=MCP_AUTH_TOKEN, body=body)

    s1, d1 = call("formula_hint", {"query": "数列求和公式有哪些"})
    t1 = tool_text(d1) if s1 == 200 else ""
    ok1 = s1 == 200 and t1.strip() and "公式" in t1
    s2, d2 = call("unit_convert", {"query": "3公里换算成米"})
    t2 = tool_text(d2) if s2 == 200 else ""
    ok2 = s2 == 200 and t2.strip() and "3000" in t2
    detail = (f"formula_hint HTTP {s1}，text={'含「公式」' if ok1 else truncate(t1) or '空'}；"
              f"unit_convert HTTP {s2}，text={'含「3000」' if ok2 else truncate(t2) or '空'}")
    return ok1 and ok2, detail


def case_login_teacher(ctx):
    status, data = req("POST", BASE + "/api/login",
                       body={"username": TEACHER, "password": TEACHER_PW})
    if status == 200 and isinstance(data, dict) and data.get("token"):
        ctx["teacher_token"] = data["token"]
        return True, f"{TEACHER} 登录成功，已取得 token"
    return False, f"HTTP {status}: {truncate(data)}"


def case_login_student(ctx):
    status, data = req("POST", BASE + "/api/login",
                       body={"username": STUDENT, "password": STUDENT_PW})
    if status == 200 and isinstance(data, dict) and data.get("token"):
        ctx["student_token"] = data["token"]
        return True, f"{STUDENT} 登录成功，已取得 token"
    return False, f"HTTP {status}: {truncate(data)}"


def case_student_register_403(ctx):
    body = {"name": "campus-tools", "base_url": MCP_BASE, "auth_token": MCP_AUTH_TOKEN}
    status, data = req("POST", BASE + "/api/mcp/servers",
                       token=ctx["student_token"], body=body)
    return status == 403, f"HTTP {status}，期望 403（学生禁止登记）: {truncate(data)}"


def case_teacher_register(ctx):
    tok = ctx["teacher_token"]
    cleaned = []
    status, servers = req("GET", BASE + "/api/mcp/servers", token=tok)
    if status == 200 and isinstance(servers, list):
        for s in servers:
            if isinstance(s, dict) and s.get("name") == "campus-tools" and s.get("id") is not None:
                ds, _ = req("DELETE", f"{BASE}/api/mcp/servers/{s['id']}", token=tok)
                cleaned.append(f"服务器#{s['id']}(HTTP {ds})")
    status2, skills = req("GET", BASE + "/api/skills", token=tok)
    if status2 == 200 and isinstance(skills, list):
        for k in skills:
            if isinstance(k, dict) and str(k.get("name", "")).startswith("公式助手") and k.get("id") is not None:
                ds, _ = req("DELETE", f"{BASE}/api/skills/{k['id']}", token=tok)
                cleaned.append(f"技能「{k['name']}」(HTTP {ds})")
    body = {"name": "campus-tools", "base_url": MCP_BASE, "auth_token": MCP_AUTH_TOKEN}
    status3, data3 = req("POST", BASE + "/api/mcp/servers", token=tok, body=body)
    if status3 == 201 and isinstance(data3, dict) and data3.get("id") is not None:
        ctx["server_id"] = data3["id"]
        extra = f"，已清理旧记录: {'、'.join(cleaned)}" if cleaned else ""
        return True, f"HTTP 201，id={data3['id']}{extra}"
    return False, f"HTTP {status3}，期望 201: {truncate(data3)}"


def case_teacher_connect(ctx):
    sid = ctx["server_id"]
    status, data = req("POST", f"{BASE}/api/mcp/servers/{sid}/connect",
                       token=ctx["teacher_token"])
    if status == 200 and isinstance(data, dict) and data.get("status") == "connected":
        tools = data.get("tools") or []
        ctx["connect_ok"] = True
        ctx["connect_tools"] = tools
        return True, f"HTTP 200，status=connected，tools={tool_names(tools)}"
    return False, f"HTTP {status}，期望 connected: {truncate(data)}"


def case_teacher_list_tools(ctx):
    tools = ctx.get("connect_tools")
    src = "connect 响应"
    if not tools:
        status, servers = req("GET", BASE + "/api/mcp/servers", token=ctx["teacher_token"])
        if status == 200 and isinstance(servers, list):
            for s in servers:
                if isinstance(s, dict) and s.get("id") == ctx["server_id"]:
                    tools = s.get("tools") or []
                    src = "GET /api/mcp/servers"
                    break
        else:
            return False, f"GET /api/mcp/servers HTTP {status}"
    names = tool_names(tools)
    ok = "formula_hint" in names
    return ok, f"来源={src}，tools={names}，{'含 formula_hint' if ok else '缺 formula_hint'}"


def case_skill_save_ok(ctx):
    body = {"name": "公式助手-apitest", "description": "数学公式与单位换算",
            "tools": ["knowledge_search", "formula_hint@campus-tools"],
            "instructions": "先调用工具获取公式清单，再作答",
            "keywords": ["公式", "换算"]}
    status, data = req("POST", BASE + "/api/skills", token=ctx["teacher_token"], body=body)
    if status != 201:
        return False, f"HTTP {status}，期望 201: {truncate(data)}"
    ctx["skill_saved"] = True
    skill_id = data.get("id") if isinstance(data, dict) else None
    if skill_id is None:
        s2, skills = req("GET", BASE + "/api/skills", token=ctx["teacher_token"])
        if s2 == 200 and isinstance(skills, list):
            for k in skills:
                if isinstance(k, dict) and k.get("name") == "公式助手-apitest":
                    skill_id = k.get("id")
                    break
    if skill_id is not None:
        ctx["skill_id"] = skill_id
        return True, f"HTTP 201，id={skill_id}"
    return True, "HTTP 201（响应未含 id，清理时按名称查找）"


def case_skill_save_bad_tool_422(ctx):
    body = {"name": "公式助手-apitest-坏工具", "description": "引用不存在的工具",
            "tools": ["ghost_tool@campus-tools"], "instructions": "不应保存成功",
            "keywords": ["公式"]}
    status, data = req("POST", BASE + "/api/skills", token=ctx["teacher_token"], body=body)
    return status == 422, f"HTTP {status}，期望 422: {truncate(data)}"


def case_skill_save_bad_server_422(ctx):
    body = {"name": "公式助手-apitest-坏服务器", "description": "引用不存在的服务器",
            "tools": ["formula_hint@no-such-server"], "instructions": "不应保存成功",
            "keywords": ["公式"]}
    status, data = req("POST", BASE + "/api/skills", token=ctx["teacher_token"], body=body)
    return status == 422, f"HTTP {status}，期望 422: {truncate(data)}"


def _find_meta(frames):
    for event, data in frames:
        if event == "meta" and isinstance(data, dict):
            return data
    return None


def _ask_check(ctx, token, question, want_status):
    """流式问答通用断言：meta.tool_calls 中存在 formula_hint@campus-tools 的指定状态。"""
    status, frames = read_sse(BASE + "/api/ask/stream", token,
                              {"question": question, "messages": []})
    if status != 200:
        return False, f"HTTP {status}，期望 200（SSE 流）"
    meta = _find_meta(frames)
    if meta is None:
        events = [e for e, _ in frames]
        return False, f"未找到 meta 帧，收到事件: {events}"
    calls = meta.get("tool_calls") or []
    hit = any(isinstance(c, dict) and c.get("tool") == "formula_hint"
              and c.get("server") == "campus-tools" and c.get("status") == want_status
              for c in calls)
    if hit and want_status == "ok":
        ctx["ask_tool_ok"] = True
    if hit and want_status == "denied":
        ctx["ask_denied_ok"] = True
    verdict = "找到" if hit else "未找到"
    return hit, f"HTTP 200，{verdict} formula_hint@campus-tools status={want_status}，tool_calls={truncate(calls)}"


def case_student_ask_tool_ok(ctx):
    return _ask_check(ctx, ctx["student_token"], "请帮我整理集合相关的公式", "ok")


def case_audit_has_ok(ctx):
    status, audit = req("GET", BASE + "/api/mcp/audit", token=ctx["teacher_token"])
    if status != 200 or not isinstance(audit, list):
        return False, f"HTTP {status}，期望 200 数组"
    hit = any(isinstance(a, dict) and a.get("status") == "ok"
              and a.get("tool") == "formula_hint"
              and a.get("server_name") == "campus-tools" for a in audit)
    verdict = "已找到" if hit else "未找到"
    return hit, f"共 {len(audit)} 条审计，{verdict} status=ok 且 tool=formula_hint、server_name=campus-tools 的记录"


def case_teacher_disconnect(ctx):
    sid = ctx["server_id"]
    status, data = req("POST", f"{BASE}/api/mcp/servers/{sid}/disconnect",
                       token=ctx["teacher_token"])
    ok = (status == 200 and isinstance(data, dict)
          and data.get("status") == "disconnected" and data.get("tools") == [])
    if not ok:
        return False, f"HTTP {status}，期望 disconnected 且 tools=[]: {truncate(data)}"
    ctx["disconnected"] = True
    extra = ""
    status2, servers = req("GET", BASE + "/api/mcp/servers", token=ctx["teacher_token"])
    if status2 == 200 and isinstance(servers, list):
        for s in servers:
            if isinstance(s, dict) and s.get("id") == sid:
                if "tool_count" in s:
                    if s["tool_count"] == 0:
                        extra = "；tool_count=0"
                    else:
                        return False, f"disconnect 成功但 tool_count={s['tool_count']}，期望 0"
                else:
                    extra = "；响应无 tool_count 字段，跳过该断言"
    return True, f"HTTP 200，status=disconnected，tools=[]" + extra


def case_student_ask_denied(ctx):
    return _ask_check(ctx, ctx["student_token"], "请帮我整理数列相关的公式", "denied")


def case_skill_save_after_disconnect_422(ctx):
    body = {"name": "公式助手-断开后-apitest", "description": "断开后保存应被拒绝",
            "tools": ["formula_hint@campus-tools"], "instructions": "不应保存成功",
            "keywords": ["公式"]}
    status, data = req("POST", BASE + "/api/skills", token=ctx["teacher_token"], body=body)
    return status == 422, f"HTTP {status}，期望 422（保存路径明确拒绝）: {truncate(data)}"


def case_audit_has_denied(ctx):
    status, audit = req("GET", BASE + "/api/mcp/audit", token=ctx["teacher_token"])
    if status != 200 or not isinstance(audit, list):
        return False, f"HTTP {status}，期望 200 数组"
    hit = any(isinstance(a, dict) and a.get("status") == "denied"
              and a.get("tool") == "formula_hint" for a in audit)
    verdict = "已找到" if hit else "未找到"
    return hit, f"共 {len(audit)} 条审计，{verdict} status=denied 且 tool=formula_hint 的记录"


def case_cleanup(ctx):
    tok = ctx["teacher_token"]
    results = []
    skill_id = ctx.get("skill_id")
    if skill_id is None and ctx.get("skill_saved"):
        s, skills = req("GET", BASE + "/api/skills", token=tok)
        if s == 200 and isinstance(skills, list):
            for k in skills:
                if isinstance(k, dict) and k.get("name") == "公式助手-apitest":
                    skill_id = k.get("id")
                    break
    if skill_id is not None:
        s, _ = req("DELETE", f"{BASE}/api/skills/{skill_id}", token=tok)
        results.append((f"技能#{skill_id}", s))
    if ctx.get("server_id") is not None:
        s, _ = req("DELETE", f"{BASE}/api/mcp/servers/{ctx['server_id']}", token=tok)
        results.append((f"服务器#{ctx['server_id']}", s))
    ok = bool(results) and all(s in (204, 404) for _, s in results)
    detail = "，".join(f"{name} HTTP {s}" for name, s in results)
    return ok, detail or "无可清理对象"


# ---------------------------------------------------------------- 调度

CASES = [
    ("mcp-health", case_mcp_health, None),
    ("mcp-no-token-401", case_mcp_no_token_401, None),
    ("mcp-bad-token-401", case_mcp_bad_token_401, None),
    ("mcp-tools-list", case_mcp_tools_list, None),
    ("mcp-tools-call", case_mcp_tools_call, None),
    ("login-teacher", case_login_teacher, None),
    ("login-student", case_login_student, None),
    ("student-register-403", case_student_register_403,
     lambda c: need((bool(c.get("student_token")), "缺少学生 token（login-student 未通过）"))),
    ("teacher-register", case_teacher_register,
     lambda c: need((bool(c.get("teacher_token")), "缺少教师 token（login-teacher 未通过）"))),
    ("teacher-connect", case_teacher_connect,
     lambda c: need((c.get("server_id") is not None, "服务器未登记（teacher-register 未通过）"))),
    ("teacher-list-tools", case_teacher_list_tools,
     lambda c: need((c.get("server_id") is not None, "服务器未登记（teacher-register 未通过）"))),
    ("skill-save-ok", case_skill_save_ok,
     lambda c: need((c.get("server_id") is not None, "服务器未登记"),
                    (bool(c.get("connect_ok")), "服务器未连接成功（teacher-connect 未通过）"))),
    ("skill-save-bad-tool-422", case_skill_save_bad_tool_422,
     lambda c: need((c.get("server_id") is not None, "服务器未登记"),
                    (bool(c.get("connect_ok")), "服务器未连接成功（teacher-connect 未通过）"))),
    ("skill-save-bad-server-422", case_skill_save_bad_server_422,
     lambda c: need((c.get("server_id") is not None, "服务器未登记"),
                    (bool(c.get("connect_ok")), "服务器未连接成功（teacher-connect 未通过）"))),
    ("student-ask-tool-ok", case_student_ask_tool_ok,
     lambda c: need((bool(c.get("student_token")), "缺少学生 token"),
                    (bool(c.get("skill_saved")), "技能未保存（skill-save-ok 未通过）"),
                    (bool(c.get("connect_ok")), "服务器未连接成功"))),
    ("audit-has-ok", case_audit_has_ok,
     lambda c: need((bool(c.get("ask_tool_ok")), "student-ask-tool-ok 未通过"))),
    ("teacher-disconnect", case_teacher_disconnect,
     lambda c: need((c.get("server_id") is not None, "服务器未登记"),
                    (bool(c.get("connect_ok")), "服务器未连接成功（teacher-connect 未通过）"))),
    ("student-ask-denied", case_student_ask_denied,
     lambda c: need((bool(c.get("student_token")), "缺少学生 token"),
                    (bool(c.get("skill_saved")), "技能未保存（skill-save-ok 未通过）"),
                    (bool(c.get("disconnected")), "服务器未断开（teacher-disconnect 未通过）"))),
    ("skill-save-after-disconnect-422", case_skill_save_after_disconnect_422,
     lambda c: need((bool(c.get("teacher_token")), "缺少教师 token"),
                    (bool(c.get("disconnected")), "服务器未断开（teacher-disconnect 未通过）"))),
    ("audit-has-denied", case_audit_has_denied,
     lambda c: need((bool(c.get("ask_denied_ok")), "student-ask-denied 未通过"))),
    ("cleanup", case_cleanup,
     lambda c: need((c.get("skill_id") is not None or bool(c.get("skill_saved"))
                     or c.get("server_id") is not None, "无可清理对象"))),
]


# ---------------------------------------------------------------- 报告与入口

def write_report(results, path=None):
    path = Path(path) if path else SCRIPT_DIR / "report.html"
    passed = sum(1 for _, s, _ in results if s == "pass")
    failed = sum(1 for _, s, _ in results if s == "fail")
    skipped = sum(1 for _, s, _ in results if s == "skip")
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = {"pass": "通过", "fail": "失败", "skip": "跳过"}
    rows = "\n".join(
        f'<tr class="{status}"><td>{html.escape(name)}</td>'
        f'<td>{label[status]}</td><td>{html.escape(detail)}</td></tr>'
        for name, status, detail in results
    )
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>CampusClaw MCP 接口测试报告</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 24px; color: #222; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .meta {{ color: #555; font-size: 13px; }}
  .stats {{ margin: 12px 0; font-size: 14px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; }}
  tr.pass {{ background: #f2fbf4; }}
  tr.fail {{ background: #fdf2f2; }}
  tr.skip {{ background: #f7f7f7; color: #777; }}
  tr.pass td:nth-child(2) {{ color: #1a7f37; font-weight: 600; }}
  tr.fail td:nth-child(2) {{ color: #c62828; font-weight: 600; }}
  tr.skip td:nth-child(2) {{ color: #9e9e9e; font-weight: 600; }}
</style>
</head>
<body>
<h1>CampusClaw MCP 接口测试报告</h1>
<p class="meta">生成时间：{generated}　|　主应用：{html.escape(BASE)}　|　MCP 服务器：{html.escape(MCP_BASE)}</p>
<p class="stats">共 {len(results)} 个用例：通过 {passed}，失败 {failed}，跳过 {skipped}</p>
<table>
<tr><th>用例</th><th>结果</th><th>详情</th></tr>
{rows}
</table>
</body>
</html>
"""
    path.write_text(page, encoding="utf-8")
    return str(path)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    missing = [name for name, val in (("MCP_AUTH_TOKEN", MCP_AUTH_TOKEN),
                                      ("SEED_TEACHER_PASSWORD", TEACHER_PW),
                                      ("SEED_STUDENT_PASSWORD", STUDENT_PW)) if not val]
    if missing:
        print("错误：缺少必需配置：" + "、".join(missing))
        print("请通过环境变量或仓库根目录 .env（KEY=VALUE 行）提供后重试。")
        return 2

    ctx = {}
    results = []
    for name, fn, dep in CASES:
        reason = dep(ctx) if dep else None
        if reason:
            results.append((name, "skip", reason))
            print(f"[跳过] {name}: {reason}")
            continue
        try:
            ok, detail = fn(ctx)
        except Exception as e:  # 单个用例异常不中断整体
            ok, detail = False, f"用例异常: {type(e).__name__}: {e}"
        status = "pass" if ok else "fail"
        results.append((name, status, detail))
        print(f"[{'通过' if ok else '失败'}] {name}: {detail}")

    passed = sum(1 for _, s, _ in results if s == "pass")
    failed = sum(1 for _, s, _ in results if s == "fail")
    skipped = sum(1 for _, s, _ in results if s == "skip")
    report = write_report(results)
    print(f"\n汇总：共 {len(results)} 个用例，通过 {passed}，失败 {failed}，跳过 {skipped}")
    print(f"报告已生成：{report}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
