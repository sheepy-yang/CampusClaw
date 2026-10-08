# CampusClaw · 迭代 1：认证授权、班级隔离与知识库入库

面向中小学教研智能体 CampusClaw 的底座：**能登录、能隔离、能上传入库**。

- 教师可上传 / 查看 / 下载**本班**教研材料（`.txt` / `.md`），上传后正文在同事务写入知识库表，为后续检索预置数据。
- 学生只能查看 / 下载。
- 班级是租户边界：跨班按 ID 访问一律 404（与"记录不存在"同形）。
- 认证、授权、隔离、上传校验全部在服务端逐请求强制执行；前端隐藏按钮不构成访问控制。

规约（SDD 四件套）：[`openspec/changes/add-auth-rbac-class-knowledge/`](openspec/changes/add-auth-rbac-class-knowledge/)
（`proposal.md` 为什么/不做什么 · `design.md` 怎么做与差异 ADR · `specs/auth-upload/spec.md` 验收 Scenario · `tasks.md` 八步任务与 verify）。

## 技术栈（零第三方依赖）

Python 3 标准库：`http.server`（同源静态前端 + JSON API）、`sqlite3`（数据）、
`hashlib` PBKDF2-HMAC-SHA256（口令加盐慢哈希）、服务端会话表（HttpOnly Cookie）。

与课件参考栈（Go + MySQL + Nginx + 三容器 Compose）的差异与安全语义等价性见
`design.md` **D10**：栈不同，但"服务端强制认证/授权/隔离、上传目录不静态暴露、
口令加盐慢哈希、登出即失效"逐条保持等价。Docker Compose 单容器交付形态见 `design.md` **D12**
（单服务 + named volume；不分 web/api/db 三容器，理由见 ADR）。本项目按课件允许的"沿用轻量栈并写清差异"执行。

## 目录结构

```text
.
├── run.py                     # 启动入口
├── .env.example               # 配置模板（复制为 .env）
├── Dockerfile                 # Python 3.12-slim，零 pip 安装
├── docker-compose.yml         # 单服务 web + named volume campusclaw-data
├── .dockerignore              # 排除 data/、.env、openspec/、.git
├── app/
│   ├── config.py              # 环境变量/.env 加载，缺密钥即退出
│   ├── db.py                  # SQLite 连接与建表
│   ├── security.py            # pbkdf2 口令哈希、会话 token
│   └── server.py              # 路由、会话、RBAC、隔离、上传入库
├── web/                       # 前端（同源静态资源，白名单提供）
│   ├── index.html
│   ├── style.css
│   └── app.js
├── scripts/
│   ├── init_db.py             # 建库 + 双班/4 账号/4 份种子材料
│   └── verify.py              # 30 条 Scenario 自动验收（自起服务）
├── data/                      # 运行期生成（已 gitignore）
│   ├── campusclaw.db
│   └── uploads/               # 原文件，只能走鉴权接口下载
└── openspec/changes/add-auth-rbac-class-knowledge/   # 变更四件套
```

## 从零运行（两步）

需要 Python 3.10+，无需 pip 安装任何包。

```bash
# 1) 准备配置并初始化数据库
copy .env.example .env            # Windows；macOS/Linux: cp .env.example .env
# 编辑 .env：填入 SESSION_SECRET（必填）与种子账号口令
#   生成密钥：python -c "import secrets;print(secrets.token_urlsafe(48))"
python scripts/init_db.py

# 2) 启动
python run.py
```

浏览器打开 <http://127.0.0.1:5000/> 。

## 用 Docker Compose 启动（单容器）

课件要求 Compose 交付；本栈因 D1 单进程同源 + SQLite 文件库，自然收敛为**单服务 + named volume**，
不分 web/api/db 三容器（强行拆分违背 D1 收益且 SQLite 无独立网络服务形态，理由见 `design.md` **D12**）。

需要本机已安装 Docker（含 Compose v2）。同样要先准备 `.env`：

```bash
copy .env.example .env            # Windows；macOS/Linux: cp .env.example .env
# 编辑 .env：填入 SESSION_SECRET（必填）与种子口令
#   生成密钥：python -c "import secrets;print(secrets.token_urlsafe(48))"

docker compose up --build         # 首次构建镜像并启动；前台日志便于排障
# 后台运行：docker compose up -d --build
```

浏览器打开 <http://127.0.0.1:5000/> 。

### 持久化语义

- named volume `campusclaw-data` 挂到容器 `/app/data`，承载 `campusclaw.db` 与 `uploads/`。
- `docker compose down` **不删 volume**，再 `up` 数据仍在；`docker compose down -v` 才清空。
- 首次启动：容器 CMD 检测 `/app/data/campusclaw.db` 不存在则跑 `python scripts/init_db.py`，已存在则跳过——
  保证 `down`/`up` 不重置种子。
- `SESSION_SECRET` 缺失时容器内 `python run.py` 退出码 1（D6 仍然成立，镜像不内置默认密钥）。

### Compose 验收

```bash
docker compose up -d --build
curl http://127.0.0.1:5000/health                          # → {"status":"ok"}
# 浏览器登录 teacher3 上传一份 .md 后：
docker compose down                                        # 不带 -v
docker compose up -d                                       # 跳过 init_db，既有数据仍在
docker volume ls | grep campusclaw-data                    # 卷可见
docker compose down -v                                     # 想清空时才加 -v
```

### 演示账号（口令在 `.env` 中设置）

| 用户名 | 角色 | 班级 | 默认口令（.env.example） |
|---|---|---|---|
| `teacher3` | 教师 | 高一(3)班 | `teacher123` |
| `student3` | 学生 | 高一(3)班 | `student123` |
| `teacher4` | 教师 | 高一(4)班 | `teacher123` |
| `student4` | 学生 | 高一(4)班 | `student123` |

## 验收：自动跑 30 条 Scenario

```bash
python scripts/verify.py
```

脚本会在独立端口（5099）自起一个服务实例，跑完自动退出，不影响 5000 上的开发服务。
覆盖课件要求的**三条失败路径**（未登录 401、学生上传 403、跨班 404）与主路径
（登录、上传后双表入库、列表可见、下载、搜索、限流、安全头、重启持久化）。

最近一次本机运行结果：**30/30 通过**。

## 关键安全行为（对应 spec.md）

- 登录失败（账号不存在 / 口令错误 / 触发限流）响应**完全同形** `401 {"error":"用户名或密码错误"}`，
  不返回 429，且账号不存在时执行哑哈希抹平耗时差异——不给账号枚举留依据。
- 会话 Cookie：`HttpOnly; SameSite=Lax`；每次请求从库重读角色与班级；登出删除服务端会话，旧 Cookie 立即失效。
- 学生调用上传接口 → 403，`materials` / `knowledge_entries` 与磁盘目录均无变化。
- 跨班详情/文件 → 404，响应体不含他班标题、正文或存储路径。
- 上传三类失败状态码：非法扩展名 415、超过 2 MiB 413、非 UTF-8 内容 422；失败时两表与磁盘无残留。
- `data/uploads/` 不是静态目录，猜测 `/uploads/<文件名>` 一律 404；下载必须经 `/api/materials/{id}/file`。
- `GET /health` 只表进程存活，不查库、不要认证；数据库故障时业务接口返回 503。
- 响应带 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、严格 CSP（无外部源）；
  正文渲染前 HTML 转义，材料中的 `<script>` 只作为文本显示。

## 接口一览

| 方法与路径 | 鉴权 | 说明 |
|---|---|---|
| `POST /api/login` | 无 | 成功 200 + Set-Cookie；失败同形 401 |
| `POST /api/logout` | 会话 | 删除服务端会话 |
| `GET /api/me` | 会话 | 角色与班级 |
| `GET /api/materials?q=` | 会话 | 本班列表 / 本班搜索 |
| `GET /api/materials/{id}` | 会话+班级 | 详情与知识库正文，跨班 404 |
| `GET /api/materials/{id}/file` | 会话+班级 | 鉴权下载，跨班 404 |
| `POST /api/materials` | 会话+教师 | 上传入库（413/415/422） |
| `GET /health` | 无 | 存活，不查库 |

## Non-goals（迭代 1 明确不做）

超级管理员、注册改密、课表与提醒、Nginx 反代 / 多副本 / HTTPS 生产部署。
Docker Compose 单实例交付（见 `design.md` D12）；多副本与 HTTPS 不做。
维护说明：材料数据可直接用 SQLite 客户端查 `data/campusclaw.db`；重置种子重跑 `python scripts/init_db.py`（容器内由 CMD 仅在首次启动时执行）。

## 迭代 2：可追溯检索与知识问答

在迭代 1 的登录、角色和班级隔离之上，材料会切成切片。教师和学生可以在本班资料里检索，并基于命中切片提问。回答带 `[1]`、`[2]`，出处可以打开对应材料。资料里没有依据时返回「资料中未找到相关内容」，并且不会调用对话模型。

规约：[`openspec/changes/add-traceable-vector-retrieval/`](openspec/changes/add-traceable-vector-retrieval/)

### 检索与降级

| 模式 | 行为 |
|---|---|
| `keyword` | 只在本班 `ready` 切片正文里按词元计数，不调用嵌入，不访问 Qdrant |
| `vector` | 问句嵌入后按余弦检索，低于 0.35 的候选丢弃 |
| `hybrid`（默认） | 两路各自过滤后按名次做 RRF（k=60） |

嵌入网关或 Qdrant 不可用时，`vector` 与 `hybrid` 返回 `503 {"error":"服务暂时不可用"}`，不编造分数。`keyword` 不受影响。问答走 hybrid：没有命中就不调用对话网关；对话网关故障同样是 503，不编造回答。

班级只认登录会话。请求里另带的 `class_id` 会被丢掉。跨班检索是 `200` 和空结果，不会用 403/404 暗示其他班有这份内容。

### 配置

在 `.env` 中增加（模板见 `.env.example`）：

- `QDRANT_URL`
- `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIM`
- `CHAT_BASE_URL` / `CHAT_API_KEY` / `CHAT_MODEL`

密钥只留在服务端。更换嵌入模型后，需要对材料执行重建索引。

```bash
docker compose up --build
# Qdrant 不映射端口：宿主机访问 6333 会被拒绝
curl http://127.0.0.1:5000/health
```

### 新增接口

| 方法与路径 | 角色 | 说明 |
|---|---|---|
| `GET /api/search?q=&mode=` | 学生 / 教师 | `keyword` / `vector` / `hybrid`，默认 hybrid |
| `POST /api/ask` | 学生 / 教师 | 混合检索前 4 条后生成带引用的回答 |
| `POST /api/materials/{id}/reindex` | 教师（本班） | 按本次策略删旧切片与旧向量后重建 |

上传接口可额外带 `strategy`（`auto` / `custom` / `hierarchy`）。未指定时按 auto：800 字窗口、80 字重叠。响应含 `chunks_total` 与 `chunks_failed`。

页面入口：登录后点「知识问答」。检索结果和回答里的出处都可以打开本班材料。
