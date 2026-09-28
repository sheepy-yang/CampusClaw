# Design: 认证授权、班级隔离与知识库入库

## Context

迭代 1 要在单机上交付 CampusClaw 的底座。课件参考实现是
浏览器 → Nginx(静态+反代) → Go(net/http + bcrypt + session) → MySQL，Docker Compose 三容器。
本项目第 2 课基线是 Python 标准库 + SQLite 的零依赖本地应用，本课沿用轻量栈并保证
安全语义逐条等价（见 D10）。认证、授权、隔离、上传校验全部发生在服务端进程；
浏览器与静态文件一律不可信。

## Goals / Non-goals

见 `proposal.md`。一句话：**访问控制在服务端强制执行；前端只改变可发现性。**

## Decisions

### D1 单进程同源架构：浏览器 → Python HTTP → SQLite

一个 `http.server.ThreadingHTTPServer` 进程同时托管 `web/` 静态资源与同源 `/api/*`。
信任边界在 API handler：所有业务接口先做认证（会话）、再做授权（角色）、再做隔离（班级）。
上传目录 `data/uploads/` 不是静态资源目录，外部无法通过路径直接访问。

**备选（否决）：** 前后端分端口/分源——引入 CORS 与 Cookie 作用域复杂度，对单机 demo 无收益。
课件中的 Nginx 反代在本栈里退化为"同进程路由"，同源语义保持一致。

### D2 服务端会话，Cookie 为 `HttpOnly; SameSite=Lax; Path=/`

登录成功后生成 256bit 随机 token，库存 token 的 SHA-256（不存明文 token），
浏览器只持有随机串。每次请求用 `secrets.compare_digest` 常量时间比对，
并每请求从库重读用户/角色/班级（不信任任何客户端声明）。会话有效期 8 小时。
登出删除服务端会话行，旧 Cookie 立即失效。

**备选（否决）：** JWT——登出后旧 token 在过期前仍有效，"登出即失效"无法判定；
课件认证方案对照页亦将服务端会话列为本迭代选择。

### D3 角色与会话属性

角色只有 `teacher` / `student`；会话三要素（user_id、role、class_id）均来自服务端库。
教师=上传/查看/下载，学生=查看/下载。未知角色默认拒绝（fail-safe）。
学生 POST 上传接口 → 403，且库与磁盘不变。

### D4 班级是租户边界（class_id 隔离）

`classes` 两个种子班：高一(3)班、高一(4)班。用户固定属于一个班，
无法通过请求参数切换。列表 SQL 必带 `WHERE class_id = ?`（参数取会话）；
按 ID 取详情/文件时先按 `id AND class_id` 查询，查不到一律返回 404，
且响应体与"记录不存在"完全同形（不含他班标题、正文、存储路径）。

### D5 口令哈希：PBKDF2-HMAC-SHA256（标准库）

`pbkdf2_hmac("sha256", password, per-user salt, 200_000)`，存
`pbkdf2_sha256$200000$<salt_hex>$<hash_hex>`。验密用 `hmac.compare_digest`；
账号不存在时也执行一次哑哈希，消除耗时差异。

**与课件差异：** 课件用 bcrypt（Go 扩展库）。本栈零第三方依赖，
PBKDF2 是 OWASP 口令防存清单认可的加盐慢哈希，迭代次数取当前建议下限 20 万；
demo 威胁模型下与 bcrypt 安全意图等价。明确禁止明文与可逆加密。

### D6 配置外置，缺失即启动失败

`SESSION_SECRET` 及种子账号口令只来自环境变量；根目录 `.env`（不入库）可注入，
`.env.example` 提供键名模板。读不到 `SESSION_SECRET` 时服务拒绝启动并打印指引，
不内置默认值。

### D7 上传与入库：先校验 → 临时文件 → 同事务双表 → 原子落盘

允许扩展名 `.txt` / `.md`（小写比对），大小上限 2 MiB，正文必须能按 UTF-8 解码。
顺序：
1. 查 Content-Length 超限 → 413（不读请求体）；
2. 解析 multipart，扩展名校验 → 415；
3. 解码 UTF-8 失败 → 422（此时尚未写库、未写最终目录）；
4. 正文先写 `data/uploads/.tmp/<随机名>`；
5. 一个事务内 INSERT `materials` 与 `knowledge_entries`（外键 `material_id`，均带 class_id）；
6. 提交成功后把临时文件原子 rename 为服务端生成名 `<material_id><ext>`；
   rename 失败则回滚事务并删除两行；任何失败路径清理临时文件。

存储名由服务端生成，原始文件名只入库用于展示与下载时 `Content-Disposition`。
知识库本迭代只存正文，为后续检索预置数据。

### D8 登录失败响应同形 + 内存限流

账号不存在、口令错误、触发限流三者返回**完全相同**的 `401 {"error":"用户名或密码错误"}`，
不返回 429（避免暴露"该账号被锁"这一枚举依据）。限流：同 IP+用户名 15 分钟内
失败 5 次后，后续尝试在窗口内一律同形 401；成功登录清零。计数器进程内存即可
（单实例；多实例部署不在本迭代）。真实原因只写服务端日志。

### D9 /health 只表存活

`GET /health` → 200 `{"status":"ok"}`，不查库、不要认证。
业务接口遇 SQLite 不可用返回 503，不把已登录用户误判为 401。

### D10 与课件参考栈的差异及等价性（ADR）

| 维度 | 课件参考栈 | 本项目 | 安全/验收语义如何保持 |
|---|---|---|---|
| 后端 | Go net/http | Python `http.server` | 同样在服务端逐请求强制认证/授权/隔离 |
| 数据库 | MySQL 8（容器内网） | SQLite 本地文件 | 同样参数化查询；文件不放入静态目录、不随接口暴露 |
| 口令 | bcrypt | PBKDF2-SHA256 20 万轮 | 同为加盐慢哈希，常量时间比对（D5） |
| 会话 | 服务端 session 表 | 服务端 sessions 表 | 登出即删行，旧 Cookie 失效（D2） |
| 入口 | Nginx 静态+反代 | 同进程路由同源 `/api` | 前端不直连数据；上传目录不静态暴露（D1） |
| 部署 | Docker Compose（web+api+db 三容器） | `python run.py` 本机直跑 与 Docker Compose 单容器（D12） | 两种启动方式等价；Compose 用 named volume 持久化，down 再 up 数据仍在 |
| 前端 | React+Vite 构建 | 原生 HTML/CSS/JS | 均为同源 fetch + Cookie；安全头与 XSS 转义不依赖框架 |

否决记录（用自己的话）：
- **vibe 一次生成全站**——无法对照 tasks 回退，规约偏离不可定位，否决；本项目按八步增量实现。
- **只写规约不发布**——第三方无法启动即不算交付，否决；提供 README 与一键验收脚本。
- **数据库映射端口/上传目录静态暴露**——默认拒绝原则下扩大攻击面，否决。

### D11 数据模型

```text
classes(id PK, name TEXT UNIQUE)
users(id PK, username TEXT UNIQUE, password_hash TEXT,
      role TEXT CHECK(role IN ('teacher','student')),
      class_id INTEGER FK -> classes.id)
sessions(id PK, token_hash TEXT UNIQUE, user_id FK,
         created_at TEXT, expires_at TEXT)
materials(id PK, class_id FK, uploader_id FK,
          title TEXT, original_name TEXT, stored_name TEXT,
          size_bytes INTEGER, content_type TEXT, created_at TEXT)
knowledge_entries(id PK, material_id FK UNIQUE, class_id FK,
                  content TEXT, created_at TEXT)
```

### D12 Docker Compose 交付：单服务 + named volume（ADR）

课件参考栈是 Compose 三容器（web 静态、api 后端、db MySQL）。本栈因 D1 单进程同源 +
SQLite 文件库，自然收敛为**单服务**：web 与 api 同进程，db 是 `/app/data` 下的文件。
强行拆三服务要么把 SQLite 套成网络服务（无意义），要么把同进程的静态与 API 拆成两容器
（违背 D1 同源收益），因此否决。

交付形态：
- `Dockerfile`（`python:3.12-slim`，零 pip 安装）+ `docker-compose.yml` 单服务 `web`。
- 数据持久化：named volume `campusclaw-data` 挂到 `/app/data`，含 `campusclaw.db` 与 `uploads/`。
  `docker compose down` 不删 volume，再 `up` 数据仍在；`docker compose down -v` 才清空。
- 首次启动：CMD 检测 `/app/data/campusclaw.db` 不存在则跑 `python scripts/init_db.py`，
  已存在则跳过——保证 down/up 不重置种子，与 §8.2 持久化语义一致。
- 环境变量：`env_file: .env` 注入 `SESSION_SECRET` 与种子口令；`environment.HOST=0.0.0.0`
  覆盖 `.env` 中的同名键（保证容器监听非回环）；缺失 `SESSION_SECRET` 时容器内
  `python run.py` 退出码 1（D6 仍然成立，不在镜像内置默认密钥）。
- 健康探针：`HEALTHCHECK` 命中 `/health`（D9，不查库）；与业务接口 503 失败路径分离。
- 端口：宿主 5000 → 容器 5000；仅单端口暴露，不映射 `/app/data` 路径。

**备选（否决）：** Compose 三服务（web/api/db）——违背 D1 同源收益，且 SQLite 无独立网络
服务形态，强行拆分只会引入跨容器复杂度而不增安全。本栈用单服务保持等价的安全语义。

**与课件"db 不映射宿主端口"约束的关系：** 课件约束针对 MySQL 这种独立网络服务；本栈
SQLite 是文件、由 named volume 承载、无独立网络端口，"不暴露宿主端口"在形态上自然成立。

### 接口一览

| 方法与路径 | 鉴权 | 成功 | 失败 |
|---|---|---|---|
| `POST /api/login` | 无 | 200 + Set-Cookie | 400 缺字段；401 同形失败 |
| `POST /api/logout` | 会话 | 204 | —（无 Cookie 也返回 204） |
| `GET /api/me` | 会话 | 200 `{username,role,class_name}` | 401 |
| `GET /api/materials?q=` | 会话 | 200 本班列表 | 401；DB 故障 503 |
| `GET /api/materials/{id}` | 会话+班级 | 200 元数据+正文 | 401 / 404（跨班同形） |
| `GET /api/materials/{id}/file` | 会话+班级 | 200 文件流 + Content-Disposition | 401 / 404 |
| `POST /api/materials` | 会话+教师 | 201 新条目 | 401/403/413/415/422 |
| `GET /health` | 无 | 200 | —（不查库） |

## Risks / Trade-offs

- SQLite 单文件 + 进程内写：demo 只读为主、上传低频，可接受；后续接检索再迁 MySQL。
- 限流状态在内存：重启清零；单实例 demo 可接受，已在 D8 写明。
- 无 HTTPS：Cookie 未带 Secure；仅限本机 HTTP 演示，生产必须置于 HTTPS 之后（Non-goal）。

## Migration Plan

Greenfield：首次运行 `python scripts/init_db.py` 建库建表并写种子。
前序课表 demo 的 `timetable.db` 不属于本应用，迁移时删除。

## Open Questions

无。
