# Spec Delta: auth-upload（认证、授权、班级隔离与材料入库）

## ADDED Requirements

### Requirement: R1 账号密码登录与统一失败响应

系统 MUST 提供 `POST /api/login`，以账号密码校验身份；登录失败响应对
"账号不存在 / 口令错误 / 触发限流"三种原因 MUST 完全同形（相同状态码 401、相同 JSON 文案、
服务端以哑哈希抹平耗时差异）。系统 MUST 对失败尝试限流，且限流命中时仍返回同形 401
（不得返回 429 暴露账号状态）。

#### Scenario: R1.1 正确凭据登录成功

- **WHEN** 未登录用户以正确用户名与口令 POST `/api/login`
- **THEN** 返回 200，响应体含 `username`/`role`/`class_name`
- **AND** 响应设置 `HttpOnly; SameSite=Lax` 的会话 Cookie

#### Scenario: R1.2 账号不存在与口令错误不可区分

- **WHEN** 分别以"不存在的账号"与"存在但口令错误的账号"发起登录
- **THEN** 两次均返回 401 与相同的 `{"error":"用户名或密码错误"}`

#### Scenario: R1.3 连续失败触发限流后仍同形

- **WHEN** 同一 IP+用户名在 15 分钟内连续失败 5 次后再次提交（即使第 6 次口令正确）
- **THEN** 在限流窗口内仍返回 401 同形响应，且不建立会话

#### Scenario: R1.4 缺字段

- **WHEN** 请求体缺少 `username` 或 `password`
- **THEN** 返回 400，且不建立会话

### Requirement: R2 角色授权（RBAC）

系统 MUST 只识别 `teacher` 与 `student` 两种角色；角色每次请求从服务端会话重读。
教师可上传、查看、下载本班材料；学生只能查看、下载。前端是否显示上传入口不构成授权。

#### Scenario: R2.1 学生上传被拒绝

- **WHEN** 学生角色会话 POST `/api/materials` 提交一份合法 .txt
- **THEN** 返回 403
- **AND** `materials`、`knowledge_entries` 行数与 `data/uploads/` 目录均无变化

#### Scenario: R2.2 学生可查看与下载

- **WHEN** 学生会话请求本班材料列表、详情与文件下载
- **THEN** 三个接口均返回 200

#### Scenario: R2.3 /api/me 返回会话身份

- **WHEN** 已登录用户 GET `/api/me`
- **THEN** 返回其用户名、角色与班级名称，供前端决定是否显示上传入口

### Requirement: R3 班级数据隔离

每个用户属于固定班级，班级取自服务端会话，MUST NOT 由请求参数指定。
列表按会话班级过滤；按 ID 访问详情或文件时，跨班与"记录不存在"响应 MUST 完全同形（404），
响应体不得包含他班材料的标题、正文或存储路径。

#### Scenario: R3.1 跨班按 ID 访问详情被拒绝

- **WHEN** 高一(3)班会话 GET `/api/materials/{高一(4)班材料ID}`
- **THEN** 返回 404，响应体不含他班标题、正文或存储路径

#### Scenario: R3.2 跨班按 ID 下载文件被拒绝

- **WHEN** 高一(3)班会话 GET `/api/materials/{他班ID}/file`
- **THEN** 返回 404，不返回任何文件字节

#### Scenario: R3.3 列表只含本班数据

- **WHEN** 教师或学生 GET `/api/materials`
- **THEN** 返回条目全部属于会话班级，不含另一班级任何标题

### Requirement: R4 材料上传与知识库入库

教师上传 `.txt` / `.md` 时，系统 MUST 校验扩展名（仅 .txt/.md）、大小（≤ 2 MiB）、
内容（UTF-8 可解码）；通过后在同一数据库事务内写入 `materials` 与 `knowledge_entries`
（两表各恰增一行，`knowledge_entries.material_id` 指向新材料且带同一 `class_id`）。
任一校验或落盘失败，两表与磁盘 MUST 无残留。存储文件名 MUST 由服务端生成。

#### Scenario: R4.1 合法上传两表各增一行

- **WHEN** 教师上传一份 1 KiB 的 `.md`
- **THEN** 返回 201 与新条目元数据
- **AND** `materials` 与 `knowledge_entries` 各新增一行并互相关联，正文已写入知识库行

#### Scenario: R4.2 上传后本班列表可见、可查库

- **WHEN** 教师上传成功后，本班任一账号 GET 列表并直接查库
- **AND** 列表出现新标题，知识库行归属本班 class_id

#### Scenario: R4.3 非法扩展名

- **WHEN** 上传 `.exe` 文件
- **THEN** 返回 415，两表与上传目录无变化

#### Scenario: R4.4 超大文件

- **WHEN** 请求声明或实际内容超过 2 MiB
- **THEN** 返回 413，两表与上传目录无变化

#### Scenario: R4.5 非法编码内容

- **WHEN** 上传扩展名为 .txt 但字节流非合法 UTF-8
- **THEN** 返回 422，两表与上传目录无变化

#### Scenario: R4.6 未登录上传

- **WHEN** 无会话 Cookie POST 上传接口
- **THEN** 返回 401，响应体不含任何业务数据

### Requirement: R5 鉴权文件访问

材料原文件 MUST NOT 作为静态资源暴露（不存在可直连的 `/uploads/...` 路径）；
下载只能经 `GET /api/materials/{id}/file`，经认证、角色（登录即可）与班级三重检查，
响应带 `Content-Disposition` 与原始文件名。

#### Scenario: R5.1 猜测静态路径取不到文件

- **WHEN** 未认证或已认证用户直接请求 `/uploads/<存储名>`
- **THEN** 返回 404，拿不到文件字节

#### Scenario: R5.2 教师/学生下载本班文件

- **WHEN** 登录用户请求本班材料文件
- **THEN** 返回 200、正确 Content-Type 与含原始文件名的 Content-Disposition

### Requirement: R6 列表与搜索来自数据库

材料列表 MUST 来自按会话班级过滤的数据库查询，不得硬编码；搜索参数 `q` 仅在本班范围内
对标题/正文做 LIKE 过滤。材料表为空时列表返回空数组而不是错误。

#### Scenario: R6.1 空表返回空列表

- **WHEN** 材料表为空，已登录用户 GET `/api/materials`
- **THEN** 返回 200 与空数组

#### Scenario: R6.2 搜索只命中本班数据

- **WHEN** 本班列表带 `q=荷塘` 查询
- **THEN** 只返回本班标题或正文命中的条目，不出现他班同名/相近材料

### Requirement: R7 凭据存储与配置外置

口令 MUST 以加盐慢哈希（PBKDF2-HMAC-SHA256，≥20 万轮，每用户独立盐）存储，
禁止明文或可逆加密；会话标识 MUST 为服务端生成的高熵随机值且库存其哈希。
`SESSION_SECRET` 与种子账号口令 MUST 来自环境变量；缺失时启动/初始化 MUST 失败，
不内置默认值。`.env` 不得入库。

#### Scenario: R7.1 数据库不存明文口令

- **WHEN** 直接查询 users 表
- **THEN** 口令字段形如 `pbkdf2_sha256$200000$<salt>$<hash>`，无明文

#### Scenario: R7.2 缺 SESSION_SECRET 启动失败

- **WHEN** 环境中无 SESSION_SECRET 时启动服务
- **THEN** 进程拒绝启动并打印配置指引

#### Scenario: R7.3 登出后旧 Cookie 失效

- **WHEN** 用户登出后持原 Cookie 请求 `/api/me`
- **THEN** 返回 401

### Requirement: R8 存活探针

`GET /health` MUST 无需认证即返回 200 表示进程存活，且 MUST NOT 查询数据库。

#### Scenario: R8.1 匿名探活

- **WHEN** 未登录 GET `/health`
- **THEN** 返回 200 `{"status":"ok"}`，过程无 SQL

#### Scenario: R8.2 数据库不可用时业务接口返回 503

- **WHEN** 数据库文件不可用且会话有效时 GET `/api/materials`
- **THEN** 返回 503，而不是 401

### Requirement: R9 Web 页面与安全呈现

系统 MUST 提供登录页与材料页；前端启动时以 `/api/me` 判定身份，任何受保护接口收到 401
后回到登录视图。401 响应体 MUST NOT 含材料标题或正文。材料正文在 DOM 渲染前 MUST 做
HTML 转义。响应 SHOULD 带 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`
与不允许外部源的 CSP。

#### Scenario: R9.1 未登录访问页面回到登录态

- **WHEN** 清空 Cookie 后打开站点
- **THEN** 显示登录页；直接调用材料接口得到 401 且无业务数据

#### Scenario: R9.2 正文脚本不被执行

- **WHEN** 材料正文含 `<script>alert(1)</script>`
- **THEN** 详情页将其作为文本显示，不执行脚本

### Requirement: R10 页面体验

页面 MUST/SHOULD 提供：北大红品牌色；浅色/深色主题切换（本地记忆）；列表/网格两种视图；
本班范围搜索；命令面板（Ctrl/Cmd+K）；上传进度条；操作结果 toast；
Markdown 材料的格式化展示（GFM 子集：标题、强调、列表、代码、引用、链接）。
这些体验条目不替代 R1–R5 的服务端强制。

#### Scenario: R10.1 主题与视图切换

- **WHEN** 用户切换深色主题与网格视图后刷新页面
- **THEN** 选择被记忆并生效

#### Scenario: R10.2 上传进度与结果反馈

- **WHEN** 教师选择文件上传
- **THEN** 上传过程显示进度条，成功/失败以 toast 反馈，成功后列表出现新条目

#### Scenario: R10.3 Markdown 格式化

- **WHEN** 查看含标题与列表的 .md 材料
- **THEN** 正文按 Markdown 结构渲染（而非整段纯文本），脚本类内容仍被转义
