# Design: 可追溯知识库检索（迭代 2）

## Context

迭代 1（`add-auth-rbac-class-knowledge`）已交付：会话登录与 RBAC、以 `class_id`
为边界的班级隔离、教师上传 `.txt`/`.md` 并在同一事务写入 `materials` 与
`knowledge_entries`（存正文原文）。栈为纯 Python 标准库 + SQLite + 原生 JS，
与课程参考栈（Go + MySQL + Nginx + Compose）的差异已在迭代 1 设计 D10 以
ADR 对照表记录。

第 4 课在其后追加检索链路：材料切分为切片，切片正文存关系库、向量存 Qdrant
（点主键与切片主键一致），提供关键字 / 向量 / 混合三种检索；检索结果与问答回答
必须可溯源到"哪份材料的哪个切片"；班级过滤在两条路径上都必须生效，班级只从
会话读取；没有命中切片时不得调用生成模型。

## Goals / Non-goals

- 目标：本班范围内可检索、可溯源（切片级引用）、无依据不凑数；关键字与向量
  双路都落实班级过滤；向量数据入专库（Qdrant）并与切片主键严格关联。
- 非目标：见 proposal「Non-goals」；另：不做多租户行级安全、不做嵌入模型训练
  或微调、不做离线包内嵌嵌入模型。

## Decisions

### D1 检索链路分层，路由层保持薄

`server.py` 只负责路由、会话装配与参数校验；切分、嵌入、向量存取、检索编排
分别放入 `app/chunking.py`、`app/embedding.py`、`app/vector_store.py`、
`app/retrieval.py`，模块间只传数据结构，不反向 import `server`。

**备选（否决）：** 全部塞进 `server.py`——迭代 1 单文件已接近可维护上限，检索
链路组件更多，混写后无法按课件逐条验收。

### D2 切分器为纯函数，三策略与课件参数一致

输入为 `body_text` 与策略参数，输出 `(chunk_text, chunk_index, char_start,
char_end)` 列表；未指定策略一律按 `auto` 处理。

- `auto`：最大 800 字、重叠 80 字，优先在空行、换行、句号处断开；请求中另行
  提交的长度与预处理参数一律忽略。
- `custom`：按所选分隔符（换行 / 空行 / 句号）切分，最大长度限 100–2000 字、
  重叠比例限 0%–50%，无断点处按最大长度强制截断；可选预处理：移除 URL 与邮箱、
  连续空白折叠为单空格；参数越界返回 422。
- `hierarchy`：按 Markdown `#`/`##`/`###` 层级分章，标题保留在该章切片内；
  单章超过 800 字再按 `auto` 规则细切。

切分只读正文：预处理仅作用于切分与嵌入输入，`knowledge_entries.body_text` 与
原始文件保持上传原样；custom 启用预处理后，字符区间相对预处理后文本计算，
不得当作原文件字符下标使用。上传与重建使用同一组参数校验。

**备选（否决）：** 语义切分（按相邻段嵌入相似度断开）——引入嵌入调用与不确定
性，课件明确按结构切分即可，且不可离线复现验收。

### D3 存储分工：切片正文只在 SQLite，向量只在 Qdrant

新增表：

```
knowledge_chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,   -- 即 Qdrant 点主键
  material_id INTEGER NOT NULL REFERENCES materials(id),
  class_id     INTEGER NOT NULL REFERENCES classes(id),
  chunk_index  INTEGER NOT NULL,
  chunk_text   TEXT NOT NULL,
  char_start   INTEGER NOT NULL,
  char_end     INTEGER NOT NULL,
  embed_status TEXT NOT NULL DEFAULT 'failed'
               CHECK (embed_status IN ('ready','failed'))
)
-- 索引：idx_chunks_class(class_id)、idx_chunks_material(material_id)
```

摘录与正文永远回 SQLite 读取；`embed_status='ready'` 才参与两类检索。
Qdrant payload 仅含 `{class_id, material_id, entry_id, chunk_id, chunk_index}`，
不含正文。

**备选（否决）：** 把切片正文冗余进 Qdrant payload——课件明确"Qdrant 仅存向量
与标识，不含正文"，冗余会造成双份正文的一致性负担，且回表校验失去意义。

### D4 向量库选 Qdrant，经 REST API 调用，零新增依赖

Qdrant 以 Compose 第二服务部署，集合 `campusclaw_chunks`，余弦距离；适配层用
标准库 `urllib` 直调其 REST API（建集合、upsert 点、带 filter 检索、按
material_id 删除点），不引入 `qdrant-client`。Qdrant 端口不映射到宿主机，
客户端无法直连；应用启动时确保集合存在（维度取 `EMBEDDING_DIM`，未配置嵌入时
跳过），Qdrant 缺失不阻塞应用启动（见 D9 降级）。

**备选（否决）：** ①向量存 SQLite BLOB + Python 暴力余弦——放弃"向量入专库"
的存储分工与按 payload 过滤的检索语义，与本次变更目标冲突；②pip 安装
qdrant-client——破坏迭代 1 起坚持的零第三方依赖约束，REST 已覆盖所需接口。

### D5 嵌入模块：OpenAI `/embeddings` 兼容网关，仅服务端调用

配置外置于 `.env`：`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、
`EMBEDDING_MODEL`、`EMBEDDING_DIM`。写入前校验"网关返回维度 = `EMBEDDING_DIM`
= 集合维度"，不一致拒绝写入；密钥仅存服务端，不出现在任何接口响应或页面中，
页面不展示向量分量。网关未配置或不可用时：新切片标记 failed，vector 与 hybrid
返回 503，keyword 与已 ready 的切片不受影响（与课件"Qdrant 不可用时关键字仍
可用"同一降级纪律）。

**备选（否决）：** 本地哈希嵌入（字符 n-gram 特征哈希）保证离线可跑——字面
匹配冒充语义向量会误导验收结论，宁可显式降级并提示配置网关。

### D6 关键字路径：词元匹配计数评分（SQLite 无 ngram 全文索引的等价实现）

问句按标点 / 空白切分为长度 ≥2 的词元并去重，在本班 `embed_status='ready'` 的
切片 `chunk_text` 上做 `LIKE '%词元%'` 计数，相关度 = Σ(出现次数 × 词元长度)；
相关度 > 0 才保留（绝对分数过滤），按相关度降序。不调用嵌入、不访问 Qdrant。

**备选（否决）：** ①FTS5 trigram 虚拟表——trigram 查询最短 3 字符，中文大量
双字词必落空；②引入 MySQL FULLTEXT ngram / 外部搜索引擎——违背迭代 1 栈
ADR。本班切片量级为数百，LIKE 计数足够，作为 ADR 等价项对照课程
`FULLTEXT ... WITH PARSER ngram(token=2)`。

### D7 排序与融合：先绝对分数过滤，再按名次 RRF（k=60）

vector 路丢弃余弦 < 0.35 的候选；keyword 路丢弃相关度 = 0 的候选；各路独立
排序后按 RRF 融合：`score = Σ 1/(60 + 名次)`，缺席一路不贡献分数；两路候选
规模相同（各取前 8）时，双路命中者得分由两路共同贡献、排序更靠前。默认
`mode=hybrid`。阈值 0.35 与 k=60 为课件标定值，作常量固化，不做配置。

**备选（否决）：** 两路分数直接相加或加权求和——两路分数量纲不同，课件明确
按名次 RRF；自定义加权需重新标定，超出本课范围。

### D8 班级隔离：会话单源 + 双侧过滤 + 回表复核

班级仅取自会话（复用迭代 1 `current_user()`）；查询串、请求体、请求头中另行
携带的班级编号解析后一律丢弃。keyword 路由 SQL 承担
`class_id = 会话班级 AND embed_status='ready'`；vector 路先在 Qdrant 带
`class_id` filter 检索，回表 SQL 再次 `AND class_id = ?`——即使单侧漏过滤，
另一侧仍取不到他班正文。跨班检索返回 200 + 空 hits（问答为"资料中未找到相关
内容"），不以 403/404 暗示他班内容存在；材料详情 / 文件沿用迭代 1 的跨班 404。

**备选（否决）：** 按班拆分 Qdrant collection——集合数随班级膨胀，payload
过滤与回表复核已互为冗余，足以满足"双重保险"。

### D9 问答编排：先检索后生成，无命中不调网关

`POST /api/ask` 仅用用户最新一句提问执行本班 hybrid 检索取前 4 条；零命中时
在检索模块直接返回 `200 {"answer":"资料中未找到相关内容","citations":[]}`，
不调用对话网关。有命中时由服务端组装上下文：材料标题、切片序号、切片正文、
本轮提问（附最近若干轮对话），system 提示由服务端写入，客户端提交的 system
消息一律丢弃；上下文不含向量分量与他班切片；对话生成组件不自行访问数据库或
向量库。对话网关为 OpenAI `/chat/completions` 兼容接口（`CHAT_*` 配置），
非流式；网关不可用返回同形 503，不编造回答。

**备选（否决）：** 无依据时也调模型并要求其自称"不知道"——约束可能被忽略，
且引入参数记忆答案，违背"无切片不调模型"的课程纪律。

### D10 溯源：citations 与 [n] 标注一一对应，摘录只出自正文库

citation 结构 `{material_id, material_title, chunk_id, chunk_index,
char_start, char_end, excerpt}`；`excerpt` 由服务端从 `chunk_text` 连续截取
（上限 160 字，超出加省略号），绝不取自向量库，也不接受客户端提交的摘录或
出处。`/api/search` 的每条 hit 即完整四要素；`/api/ask` 的 citations 顺序与
回答中 [1][2]… 首次出现顺序一致，前端出处列表按此顺序渲染，点击出处经迭代 1
详情 / 文件接口打开材料（跨班 material_id 返回 404）。

**备选（否决）：** 引用只给材料标题不给切片序号 / 字符区间——追溯粒度退化到
整篇，无法定位到段，不满足"可追溯"要求。

### D11 入库四步、失败语义与重建索引

上传事务（迭代 1）提交后同步执行：①按策略切分并写 `knowledge_chunks`
（`embed_status='failed'` 初值）→ ②逐片调用嵌入 → ③成功者 upsert Qdrant 点
并置 `ready` → ④失败者保持 `failed`。材料记录永不因索引失败而删除；重建是
唯一的补齐路径：`POST /api/materials/{id}/reindex`（教师，本班）先按
material_id 删除该材料全部 Qdrant 点与切片行，再按本次请求的策略重跑①–④，
保证幂等且无旧点残留。已入库材料不自动重切，更换策略必须显式重建。
种子材料在 `init_db.py` 中按 auto 策略补齐切片；配置了嵌入网关时同时完成
向量写入，否则保持 failed 等待重建。

**备选（否决）：** 后台队列异步嵌入——单进程部署引入任务状态机与并发写 SQLite
的复杂度；2MiB 上限、数百切片规模下同步入库延迟可接受。

### D12 栈差异 ADR 对照（延续迭代 1 D10）

| 环节 | 课程参考栈 | 本项目 | 等价性说明 |
|---|---|---|---|
| Web 层 | Nginx + Go | Python `http.server` 直连 | 沿用迭代 1 D10 结论 |
| 关系库 | MySQL FULLTEXT ngram(token=2) | SQLite + 词元 LIKE 计数 | 中文词面匹配与相关度排序等价，见 D6 |
| 向量库 | Qdrant（Compose） | Qdrant（Compose，REST 直调） | 完全一致，仅客户端实现不同 |
| 嵌入 | 课程网关 `/embeddings` | OpenAI 兼容网关（配置外置） | 同一接口约定，密钥均不下发浏览器 |
| 对话 | 课程对话网关 | OpenAI 兼容网关（配置外置） | 同上，均仅在有依据后被调用 |
| 编排 | Go 自实现，不引框架 | Python 自实现，不引框架 | 便于对照规约逐条验收 |

### 接口一览（新增 / 扩展）

| 方法与路径 | 角色 | 行为要点 |
|---|---|---|
| `GET /api/search?q=&mode=` | 学生 / 教师 | 三模式检索，默认 hybrid；空查询 400；未登录 401 同形；向量 / 混合依赖不可用 503 同形 |
| `POST /api/ask` | 学生 / 教师 | 混合 top-4 → [n] 标注回答 + citations；无命中固定文案；网关不可用 503 |
| `POST /api/materials/{id}/reindex` | 教师（本班） | 按本次策略重建切片与向量索引；学生 403、跨班 404 |
| `POST /api/materials`（扩展） | 教师 | multipart 增加可选 `strategy` 与 custom 参数字段；响应含切片统计 |

## Risks / Trade-offs

- **外部网关依赖**：嵌入 / 对话网关缺失或故障时，向量、混合与问答降级或 503；
  验收环境需提供课程网关配置，keyword 与已 ready 切片始终可用。
- **LIKE 计数非真全文索引**：词面无关的同义改写由 vector 路兜底；数据量增长后
  需换 FTS5 / 外部引擎——检索实现已隔离在 `retrieval.py`，可整体替换。
- **双库一致性**：入库顺序"先切片行后向量点"，崩溃窗口可能留下 failed 切片，
  以重建兜底；重建先删点后删行，保证无旧点残留（点 ID 复用自增主键，必须先删）。
- **阈值与融合参数**：0.35 / k=60 按课件标定，更换嵌入模型须重新评估并重建全部
  索引（固化常量，避免误配）。
- **同步入库延迟**：上传响应时间随切片数线性增长；2MiB / 800 字窗口下切片数
  ≤ 数十，延迟可接受。

## Migration Plan

1. SQLite 启动时 `CREATE TABLE IF NOT EXISTS knowledge_chunks` 与索引；
   `materials` / `knowledge_entries` 不改结构，旧数据不受影响。
2. `docker-compose.yml` 增加 `qdrant` 服务与数据卷（端口不对外）；应用启动时
   幂等创建集合（维度 = `EMBEDDING_DIM`）。
3. `.env` 增量配置；缺 `SESSION_SECRET` 仍启动失败，缺嵌入 / 对话配置仅降级。
4. `init_db.py` 幂等：仅对尚无切片的种子材料补切片（有网关时含向量）。
5. 前端新增检索 / 问答入口，迭代 1 页面与行为不变。

## Open Questions

无。
