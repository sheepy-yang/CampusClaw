# Tasks: 认证授权、班级隔离与知识库入库

> 每个 task 一轮：实现 → 执行 verify → 勾选 `- [x]`。verify 均为可执行命令/接口调用。
> 工作目录为项目根；服务默认 http://127.0.0.1:5000 ；验收脚本自起 5099。
> 勾选证据：`scripts/verify.py` 30/30 通过；缺密钥启动失败、重启持久化、浏览器交互均人工实测；Docker Compose 构建启动与 down/up 持久化本机实测通过（8.4–8.6）。

## §1 项目骨架与配置

- [x] 1.1 建立 `app/`、`web/`、`scripts/`、`data/` 目录与 `.env.example` — verify: 目录齐备；`.gitignore` 忽略 `.env` 与 `data/`
- [x] 1.2 配置加载器 `app/config.py`：读取环境变量/根目录 `.env`，缺 `SESSION_SECRET` 抛错退出 — verify: 移走 `.env` 后 `python run.py` 退出码 1 并打印配置指引（实测）
- [x] 1.3 服务骨架与 `/health`（不查库、不认证） — verify: verify.py T0.1 返回 `{"status":"ok"}`

## §2 数据层与种子

- [x] 2.1 SQLite 建表：classes/users/sessions/materials/knowledge_entries（外键、CHECK 约束） — verify: 初始化成功；verify 多表 JOIN/外键关联查询通过（T8.2）
- [x] 2.2 种子：双班、4 账号（双班各 1 教师 1 学生），口令来自环境变量并以 pbkdf2 哈希入库 — verify: 4 账号均可登录；查库 password_hash 形如 `pbkdf2_sha256$200000$...`（重启核验脚本实测）
- [x] 2.3 种子材料：两班各 2 份标题可区分的 .txt/.md，并写 knowledge_entries — verify: T4.1（3 班 2 条）、T15.1（4 班搜「荷塘」0 命中，反向证明标题按班区分）

## §3 登录、会话与限流

- [x] 3.1 `POST /api/login` 成功签发 HttpOnly+SameSite=Lax Cookie；失败统一 401 文案 — verify: T1.1/T1.2/T1.3/T1.4
- [x] 3.2 服务端 sessions 表、库存 token 哈希、8 小时有效期；每请求从库重读角色/班级 — verify: T1.2 Cookie 属性；/api/me 与跨班用例均以库读身份通过
- [x] 3.3 `POST /api/logout` 删服务端行，旧 Cookie 立即失效 — verify: T2.2
- [x] 3.4 失败限流：同 IP+用户名 15 分钟 5 次后同形 401（不返回 429） — verify: T3（6 次全 401，第 6 次口令正确仍拒绝）

## §4 班级隔离

- [x] 4.1 列表 SQL 按会话 class_id 过滤 — verify: T4.1
- [x] 4.2 详情按 `id AND class_id` 查询，跨班与不存在同形 404 — verify: T5（响应字节一致，不含他班标题）
- [x] 4.3 文件下载同样按班级核对，跨班 404 无字节 — verify: T6

## §5 角色授权与上传入库

- [x] 5.1 学生 POST 上传 → 403，两表行数与上传目录不变 — verify: T7（before/after=(4,4,4,0)）
- [x] 5.2 教师上传合法 .md → 201；双表同事务各增一行且关联、同班 — verify: T8.1–T8.3
- [x] 5.3 三类校验：.exe→415、>2MiB→413、非法 UTF-8→422，失败均无残留 — verify: T9/T10/T11
- [x] 5.4 下载走鉴权接口、Content-Disposition 带原始名；`/uploads/...` 不存在 — verify: T12/T13
- [x] 5.5 未登录调用受保护接口 → 401 且响应体无业务数据 — verify: T0.2/T14

## §6–§7 材料读取 API 与前端页面

- [x] 6.1 `/api/me`、列表（含 q 本班搜索）、详情、空结果返回 [] — verify: T2.1/T15.1（无命中返回 `[]`，空表同路径）/T15.2
- [x] 6.2 登录页 + 材料页；401 回登录；导航依据 /api/me；学生无上传入口 — verify: 浏览器实测（错误密码红条、教师/学生两种顶栏、控制台无 JS error）
- [x] 6.3 北大红品牌、浅/深色（记忆）、列表/网格（记忆）、本班搜索 — verify: 浏览器实测截图
- [x] 6.4 命令面板 Ctrl/Cmd+K、上传进度条、toast — verify: 浏览器实测（面板开合；上传进度/toast 由 T8 主路径与 XHR 实现覆盖）
- [x] 6.5 Markdown GFM 子集渲染 + 正文 HTML 转义 + 安全响应头 — verify: T16.1（服务端原文入库）、T0.3（nosniff/DENY/CSP）、浏览器确认 `.md` 标题/列表格式化；`escapeHtml` 先转义再渲染、CSP 禁内联脚本（R9.2 双保险）

## §8 复现与发布（含 Docker Compose 交付）

- [x] 8.1 README 写清：配置 `.env` → `python scripts/init_db.py` → `python run.py` → 浏览器地址 — verify: 按 README 在本机从零执行通过
- [x] 8.2 数据持久：停服再启动，账号与材料仍在 — verify: 重启后登录 200、3 班列表 2 条、哈希形态不变（实测）
- [x] 8.3 运行期目录不被静态映射；data/ 下数据库与上传目录无直连 URL — verify: T13 + 白名单式静态路由代码审查（仅 `/`、`/static/*`）
- [x] 8.4 `Dockerfile`（`python:3.12-slim`，仅复制 `app/`、`web/`、`scripts/`、`run.py`，`HOST=0.0.0.0`，`/health` 探针） — verify: `docker compose build` 成功；镜像不含 `data/`、`.env`、`openspec/`（本机 Docker 实测通过）
- [x] 8.5 `docker-compose.yml` 单服务 `web` + named volume `campusclaw-data` + `env_file: .env` + 端口 5000 — verify: `docker compose up -d` 后 `curl http://127.0.0.1:5000/health` 返回 `{"status":"ok"}`；浏览器可见登录页（本机实测通过）
- [x] 8.6 `down` 再 `up` 数据持久；首次启动建库、后续跳过 — verify: 登录上传后 `docker compose down`（不带 `-v`）→ `up`，账号与材料仍在；`docker volume ls` 可见 `campusclaw-data`（本机实测通过）

## §9 发布验收

- [x] 9.1 四件套齐全（proposal/design/tasks/spec），每条 Requirement 至少一个 Scenario — verify: 人工对照，R1–R10 均有 Scenario
- [x] 9.2 `python scripts/verify.py` 全绿（主路径 + 三条失败路径：未登录、学生 403、跨班 404） — verify: **30/30 通过，退出码 0**
- [x] 9.3 关键 Scenario 判定结论写入迭代说明 — verify: README「验收」节记录 30/30 与三条失败路径
