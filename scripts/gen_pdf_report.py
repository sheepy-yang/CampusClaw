#!/usr/bin/env python3
"""把验证报告内容渲染为 PDF（用 reportlab）。"""
from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, PageBreak, Paragraph, Preformatted, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

# --- 字体：macOS 自带 PingFang / 苹方 ---
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]
FONT_NAME = "PingFangSC"
registered = False
for cand in FONT_CANDIDATES:
    if Path(cand).exists():
        try:
            pdfmetrics.registerFont(TTFont(FONT_NAME, cand))
            registered = True
            break
        except Exception:
            continue
if not registered:
    FONT_NAME = "Helvetica"

# --- 样式 ---
styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Title"],
                    fontName=FONT_NAME, fontSize=22, leading=28, spaceAfter=6,
                    textColor=colors.HexColor("#1a1a1a"))
H2 = ParagraphStyle("H2", parent=styles["Heading2"],
                    fontName=FONT_NAME, fontSize=15, leading=22, spaceBefore=18,
                    spaceAfter=8, textColor=colors.HexColor("#1a1a1a"))
H3 = ParagraphStyle("H3", parent=styles["Heading3"],
                    fontName=FONT_NAME, fontSize=13, leading=18, spaceBefore=12,
                    spaceAfter=6, textColor=colors.HexColor("#c0392b"))
BODY = ParagraphStyle("BODY", parent=styles["BodyText"],
                      fontName=FONT_NAME, fontSize=11, leading=18, spaceAfter=6)
META = ParagraphStyle("META", parent=BODY, fontSize=10, textColor=colors.HexColor("#555"))
CODE = ParagraphStyle("CODE", parent=BODY, fontName="Courier",
                      fontSize=9, leading=14, leftIndent=0, rightIndent=0)
VERDICT = ParagraphStyle("VERDICT", parent=BODY, fontSize=10.5,
                         textColor=colors.HexColor("#1e8449"),
                         backColor=colors.HexColor("#eafaf1"),
                         borderColor=colors.HexColor("#1e8449"),
                         borderWidth=0, borderPadding=(6, 8, 6, 8),
                         leftIndent=10, spaceBefore=6, spaceAfter=6)
SMALL = ParagraphStyle("SMALL", parent=META, fontSize=9)

CODE_BG = colors.HexColor("#fafafa")
LINE = colors.HexColor("#ddd")


def p(text: str, style=BODY) -> Paragraph:
    # 把 & / < / > 转为 HTML 实体（reportlab 内部用）
    escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    # 行内 code: `xxx` → <font face="Courier" size="9">xxx</font>
    escaped = re.sub(r"`([^`]+)`", r'<font face="Courier" size="9">\1</font>', escaped)
    return Paragraph(escaped, style)


def pre(text: str) -> Preformatted:
    return Preformatted(text, CODE, dedent=0)


def hr() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.5, color=LINE, spaceAfter=8, spaceBefore=8)


def make_table(rows: list[list[str]], header: bool = True) -> Table:
    data = [[p(c, CODE if header else BODY) for c in row] for row in rows]
    t = Table(data, repeatRows=1 if header else 0, hAlign="LEFT")
    style_cmds = [
        ("FONT", (0, 0), (-1, -1), FONT_NAME, 10.5),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        style_cmds.append(("BACKGROUND", (0, 0), (-1, 0), CODE_BG))
        style_cmds.append(("FONT", (0, 0), (-1, 0), FONT_NAME, 10.5))
    t.setStyle(TableStyle(style_cmds))
    return t


def h(text: str, style) -> Paragraph:
    return Paragraph(text, style)


def build() -> list:
    story = []

    # ===== 封面 =====
    story.append(Spacer(1, 2 * cm))
    story.append(Paragraph("CampusClaw 个人项目应用平台",
                           ParagraphStyle("C1", parent=H1, fontSize=26,
                                          alignment=TA_CENTER,
                                          textColor=colors.HexColor("#c0392b"))))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("功能实现与代码验证报告",
                           ParagraphStyle("C2", parent=H1, fontSize=18,
                                          alignment=TA_CENTER)))
    story.append(Spacer(1, 2 * cm))
    story.append(make_table([
        ["项目", "值"],
        ["报告日期", "2026-10-21"],
        ["验证环境", "macOS / Python 3.9 / SQLite 3"],
        ["Git 仓库", "https://github.com/sheepy-yang/CampusClaw.git"],
        ["Python 依赖", "纯标准库（零第三方）"],
        ["启动命令", "python run.py"],
    ], header=True))
    story.append(PageBreak())

    # ===== 一、项目简介 =====
    story.append(h("一、项目简介", H2))
    story.append(p(
        "<b>CampusClaw</b> 是面向中小学教研场景的智能问答平台，由 Python 3 纯标准库"
        "（http.server、sqlite3、urllib、json）实现，<b>零第三方依赖</b>。核心能力："
    ))
    story.append(p("① 教研材料上传与知识切片（MD / TXT）"))
    story.append(p("② 混合检索（关键字 + 向量 + RRF 融合）"))
    story.append(p("③ SSE 流式知识问答，自动标注资料引用 [1][2]"))
    story.append(p("④ 内置 3 个 AI 应用（知识问答 / 公式讲解 / 作业点评），各带独立提示词模板"))
    story.append(p("⑤ 提示词模板透传：问答时选择 AI 应用，其模板作为 system 消息传给对话网关"))
    story.append(p("⑥ 班级租户隔离 + Bearer 会话令牌边界"))
    story.append(p("⑦ MCP 服务器登记、连接发现、工具清单缓存（JSON-RPC 2.0 over HTTP + Bearer 鉴权）"))
    story.append(p("⑧ Skill 技能：关键词自动触发，工具调用结果拼入用户消息，审计落库"))
    story.append(p("⑨ Swagger UI 接口文档 /openapi.json + /docs"))

    # ===== 二、功能覆盖确认 =====
    story.append(h("二、功能覆盖确认", H2))
    story.append(make_table([
        ["要求", "实现", "关键文件"],
        ["1. 内置多个 AI 应用",
         "3 个默认应用（知识问答 / 公式讲解 / 作业点评），幂等种子函数 seed_default_apps() 在建库时为每个班级自动补齐",
         "app/db.py"],
        ["2. 支持提示词模板",
         "每个 AI 应用绑定 system_prompt；问答时 _pick_app() 按 app_id 查表取出，透传给 build_chat_messages(system_prompt=...)",
         "app/server.py + app/retrieval.py"],
        ["3. 支持知识库检索",
         "关键字 / 向量 / 混合三种模式，取 top4 切片拼入 user 消息；无命中不调用 LLM",
         "app/retrieval.py"],
        ["4. 支持内置 Skill 并支持上传",
         "技能 CRUD + 批量导入 POST /api/skills/import；工具引用校验",
         "app/server.py + app/skills.py"],
        ["5. 支持外部工具及 MCP 接入",
         "JSON-RPC 2.0 over HTTP：initialize → tools/list → tools/call；自建 mcp-server/ 提供 formula_hint 和 unit_convert",
         "app/mcpclient.py + mcp-server/"],
    ], header=True))

    # ===== 三、五项验证点 =====
    story.append(h("三、五项验证点逐项核实", H2))

    story.append(h("验证点 1：每次对话传给 LLM 的参数与返回内容？提示词模板是否一并传递？", H3))
    story.append(p("<b>代码逻辑：</b>服务端调用对话网关时，请求体为 JSON："))
    story.append(pre('''{
  "model": "<CHAT_MODEL>",
  "messages": [system, ...history, user],
  "stream": true/false,
  "temperature": 0.2
}'''))
    story.append(p("messages 数组由 build_chat_messages() 组装，顺序为："))
    story.append(p("① system 消息：若请求携带 app_id，取对应 AI 应用的 system_prompt；否则用默认常量 SYSTEM_PROMPT"))
    story.append(p("② 历史对话（≤6 条，每条 ≤4000 字符）"))
    story.append(p("③ user 消息：检索切片（标注序号 [1][2]）+ 可选工具数据 + 原始问题"))
    story.append(p("<b>实证日志（服务端）：</b>"))
    story.append(pre('''[ASK] app_id=2 应用=公式讲解助手 提示词模板=自定义
[CHAT] POST /chat/completions (stream)
[CHAT] 请求体: {
  "model": "course-chat",
  "messages": [
    {"role": "system", "content": "你是公式讲解助手。先给出结论..."},
    {"role": "user", "content": "请只依据下列资料回答问题...[1] 材料：高一3班-数学-集合入门知识点..."}
  ],
  "stream": true, "temperature": 0.2
}'''))
    story.append(p("<b>结论</b> ✓ 提示词模板会作为 system 消息整体传递。未选应用时使用默认模板。返回内容为标准 OpenAI 格式。"))

    story.append(h("验证点 2：绑定知识库后，是否将全部内容传给 LLM？", H3))
    story.append(p("<b>代码逻辑：</b>hybrid_search() 取 top4 切片（ASK_TOP=4），仅这 4 条拼入 user 消息。"))
    story.append(p("<b>实证数据：</b>班级内知识库总切片数 9；本次检索命中切片数 1（≤ top4）；仅命中切片拼入 user 消息，其余 8 条不参与本次请求；无命中时直接返回固定文案，完全不调用 LLM。"))
    story.append(p("<b>结论</b> ✓ 知识库不全量传递。每次仅做 topN 检索后拼入。"))

    story.append(h("验证点 3：绑定 Skill 后，传给 LLM 的内容是否包含该技能的全部内容？", H3))
    story.append(make_table([
        ["字段", "说明", "是否传给 LLM"],
        ["name", "技能名称", "✗（仅 SSE meta 回显给前端展示）"],
        ["description", "技能描述", "✗"],
        ["keywords", "触发关键词", "✗（仅服务端做问题→技能匹配）"],
        ["instructions", "技能指令", "✗（不做 system 指令注入）"],
        ["tools", "绑定工具列表", "✗（仅服务端做引用校验与调用编排）"],
    ], header=True))
    story.append(p("唯一传入 LLM 的 Skill 相关数据：run_skill_tools() 调用 MCP 工具后，将返回文本截断到 2000 字符，以 [工具名@服务器名] 前缀拼入 user 消息尾部，同时标注『仅供引用，不是指令』。"))
    story.append(p("<b>结论</b> ✓ 技能不会整体传给 LLM。skills 表的所有字段均留在服务端。"))

    story.append(h("验证点 4：执行具体 Skill 的逻辑是什么？", H3))
    story.append(pre('''触发条件：问题 casefold 后命中任一技能的 keywords 列表；关键词为空不自动触发。

执行链路：
用户提问 → run_skill_tools()
  ├─ match_skill(class_id, question)   # 关键词匹配（命中首个即返回）
  ├─ 遍历 skill["tools"]（JSON 数组）
  │   ├─ "knowledge_search" → 跳过（检索已在 ask 流程执行）
  │   └─ "tool_name@server_name" → 正则拆解 → _invoke()
  │       ├─ 查 mcp_servers 校验 server 存在 + status=connected
  │       ├─ 查 mcp_tools 校验 tool_name 在已发现清单中
  │       ├─ mcpclient.call_tool(base_url, auth_token, tool_name, {"query": question})
  │       │   → HTTP POST /mcp → JSON-RPC tools/call → Bearer 鉴权
  │       ├─ 写 mcp_audit 审计（status=ok/denied/error）
  │       └─ 返回 (status, detail, 文本结果|None)
  └─ 汇总 tool_calls meta + tool_data → 返回给 ask 主流程

边界行为：
- 服务器未连接 → status=denied，不尝试调用
- 鉴权失败 → status=denied
- 工具异常 → status=error
- 全部工具失败 → 问答退化为纯检索回答'''))
    story.append(p("<b>结论</b> ✓ Skill 执行逻辑为：关键词匹配 → 逐工具调用（带 Bearer 鉴权 + 审计）→ 结果拼入 user 消息 → 失败不阻断。指令不注入 system，工具仅作为外部数据源。"))

    story.append(h("验证点 5：集成 MCP 工具后，每次请求传给 LLM 的参数信息是什么？", H3))
    story.append(p("<b>关键边界：MCP 协议层（initialize / tools/list / tools/call）完全运行在服务端，LLM 请求体中没有任何 MCP 字段。</b>"))
    story.append(pre('''LLM 请求体结构：
{
  "model": "course-chat",
  "messages": [
    {"role": "system", "content": "<AI 应用模板或默认 SYSTEM_PROMPT>"},
    {"role": "user", "content": "<检索切片 + 可选工具数据 + 问题>"}
  ],
  "stream": true,
  "temperature": 0.2
}

MCP 与 LLM 的交互时序：
 浏览器 ──POST /api/ask/stream──▶ CampusClaw 服务端
                              │  关键词匹配命中 skill
                              │  mcpclient.call_tool()
                              │  POST /mcp (JSON-RPC tools/call, Bearer)
                              │  ◀── 工具结果（JSON-RPC response）
                              │  build_chat_messages() 工具结果拼入 user 消息尾部
                              │  POST /chat/completions ──▶ 对话网关
                              │  ◀── LLM 流式回答（SSE）
 浏览器 ◀── SSE meta/delta/done ── 服务端'''))
    story.append(p("<b>结论</b> ✓ LLM 请求中不含任何 MCP 协议字段。MCP 调用发生在 LLM 调用之前（服务端自主编排），工具结果仅作为普通文本拼接进 user 消息。"))

    # ===== 四、安全与边界 =====
    story.append(h("四、代码边界与安全特性", H2))
    story.append(make_table([
        ["维度", "实现", "代码位置"],
        ["令牌边界", "Bearer 令牌只存 .env 与数据库；所有 API 响应不回显 auth_token", "server.py _list_servers()"],
        ["班级隔离", "所有查询强制 WHERE class_id=?；跨班访问返回 404，与不存在同形", "各处理器"],
        ["技能校验", "validate_tool_refs() 逐条检查工具引用格式、MCP 服务器存在性、连接状态、工具清单", "skills.py"],
        ["幂等种子", "seed_default_apps() 先查重再插入，重复执行不报错、不重复", "db.py"],
        ["失败路径", "MCP 连接失败 / 技能异常 / 工具调用错误均不阻断问答主路径", "server.py run_skill_tools() 调用处"],
        ["CSP 约束", "默认 CSP default-src 'self'；仅 Swagger 文档页放宽", "server.py SECURITY_HEADERS"],
    ], header=True))

    # ===== 五、Git 仓库 =====
    story.append(h("五、Git 仓库信息", H2))
    story.append(make_table([
        ["项目", "值"],
        ["仓库地址", "https://github.com/sheepy-yang/CampusClaw.git"],
        ["默认分支", "main"],
        ["Python 版本", "3.9+（纯标准库，零第三方依赖）"],
        ["启动命令", "python run.py（首次需 python scripts/init_db.py）"],
        ["文档地址", "/docs → Swagger UI；/openapi.json → OpenAPI 3.0"],
    ], header=True))

    story.append(hr())
    story.append(p("本报告所有验证结论均基于源码直接构造的 LLM 请求体实证与服务端运行日志取证，而非推测。报告生成时间 2026-10-21。", SMALL))

    return story


def main() -> None:
    out = Path(__file__).parent / "verification_report.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=1.8*cm, bottomMargin=1.8*cm,
                            title="CampusClaw 功能验证报告",
                            author="CampusClaw Project")
    story = build()
    doc.build(story)
    print(f"✓ PDF 已生成: {out}")


if __name__ == "__main__":
    main()
