# Proposal: 认证授权、班级隔离与知识库入库（迭代 1）

## Why

CampusClaw 是面向中小学的教研智能体，后续智能能力（检索、问答、命题）的前提是：
知道**谁**在访问、能做**什么**、只能看**哪个班**的数据，并且教研材料已经可靠入库。
本迭代交付底座：登录与角色权限、班级数据边界、教研材料上传与知识库正文入库。
判定标准不是界面好看，而是：能登录、能隔离、能上传入库、第三方按 README 可复现。

## What Changes

- 新增账号密码登录、服务端会话（HttpOnly Cookie）、登出。
- 两种角色：教师（上传/查看/下载本班材料）、学生（仅查看/下载）。
- 班级作为租户边界：用户属于固定班级，服务端按会话 `class_id` 过滤数据；跨班按 ID 访问返回 404。
- 教师上传 `.txt` / `.md`：校验扩展名、大小、编码；在同一事务写入 `materials` 与 `knowledge_entries`；失败无残留。
- 材料原文件仅能通过鉴权接口下载，不做静态目录暴露。
- 前端：登录页 + 材料页；`/api/me`、401 回登录、正文 XSS 安全、北大红品牌、深浅色、列表/网格、本班搜索、命令面板、上传进度、toast、Markdown 渲染。
- 存活探针 `/health`（不查库、不要认证）。
- 技术栈差异（相对课件参考栈 Go + MySQL + Nginx + Compose）：
  **Python 3 标准库（http.server + sqlite3 + hashlib pbkdf2）+ SQLite，单进程同源服务**，
  同源提供静态前端与 `/api`；本机 `python run.py` 或 Docker Compose 单容器均可启动，
  理由与等价性见 `design.md` D10、Compose 交付形态见 D12。

## Non-goals

- 不做向量检索、全文搜索引擎、RAG 问答与对话接口（本班列表 `LIKE` 筛选不算检索）。
- 不做超级管理员角色及其跨班运维、操作审计。
- 不做注册、找回密码、改密码、个人资料管理。
- 不做换课、课表、提醒、iCal 导出（前序课表 demo 与本应用无关，不迁入）。
- 不做 Nginx 反代 / 多副本 / 生产部署与 HTTPS 终止（本机或单容器 HTTP demo；
  Docker Compose **本迭代交付**单实例，见 `design.md` D12；多副本与 HTTPS 不做）。
- 不做前端绕过能力：隐藏按钮不是访问控制，一切以后端强制为准。

## Impact

- 新增 `app/`（服务端）、`web/`（静态前端）、`scripts/`（初始化与验收脚本）。
- 运行期数据：`data/campusclaw.db`（SQLite）、`data/uploads/`（原文件，仅进程内访问）。
- 配置来自环境变量（可由根目录 `.env` 注入，`.env` 不入库）：`SESSION_SECRET`、种子口令等。
- README 给出从零复现命令。
