# Proposal: 可追溯知识库检索（迭代 2）

## Why

迭代 1 解决了"谁在访问、能做什么、只能看哪个班"，并把教研材料正文可靠地存进了
`knowledge_entries`，但系统还回答不了"这份材料里哪一段讲了什么"：教师想找
"某次月考的命题要求"，只能靠列表页的 `LIKE` 筛选人工翻找。第 4 课在此基础上
引入检索能力：把材料切分为切片，用自然语言在本班材料中定位相关片段，每条结果
都标注出处、可回溯到原文；资料中没有依据时，必须明确返回"未找到"，不得编造
出处，也不得以相关度明显偏低的切片凑数。

## What Changes

- 新增切片层：上传成功后按所选策略（自动窗口 `auto` / 自定义分隔符 `custom` /
  按标题分章 `hierarchy`）把 `knowledge_entries.body_text` 切分为
  `knowledge_chunks`，记录切片序号与字符区间；切分只读正文，原文与
  `knowledge_entries.body_text` 保持上传原样，预处理不改写原文。
- 新增向量存储：引入 Qdrant（Compose 部署，仅存向量与标识，不含正文），切片与
  问句向量由 OpenAI `/embeddings` 兼容网关在服务端生成，密钥不下发浏览器；
  向量点主键与 `knowledge_chunks.id` 一致，payload 只含 class_id、material_id、
  entry_id、chunk_id、chunk_index。
- 新增检索接口 `GET /api/search`：支持 keyword（仅查切片正文，不调嵌入、不访问
  向量库）、vector（问句嵌入后余弦检索，相似度低于 0.35 的候选丢弃）、hybrid
  （两路各自过滤后按名次 RRF 融合，默认模式）三种模式；向量库或嵌入网关不可用
  时向量与混合模式返回同形 503，关键字模式不受影响。
- 新增问答接口 `POST /api/ask`：先以用户最新一句提问做本班混合检索取前 4 条切片，
  仅在有命中切片时才调用对话网关生成带 [1][2] 引用标注的简短回答；没有任何命中
  时直接返回固定文案「资料中未找到相关内容」，citations 为空，不调用生成模型。
- 检索侧班级隔离：班级标识只从登录会话读取，请求中另行携带的班级编号解析后一律
  丢弃；关键字与向量两条路径都必须按本班过滤，向量命中后回表取正文时再次校验
  班级；跨班检索对外表现为 200 + 空命中，不以 403/404 泄露其他班内容的存在性。
- 内容溯源：每条检索结果与问答引用都包含材料标题、切片序号、字符区间、摘录
  四要素，摘录由服务端截取自切片正文（而非向量库），并可打开对应材料。
- 教师可对本班材料重建索引：先删除旧切片与旧向量点，再按本次请求的策略重新
  切分、嵌入、写入；嵌入失败的切片标记 failed，材料记录保留。
- 前端新增检索与问答页面：三种模式切换，结果卡片展示出处四要素，回答中的
  [1][2] 标注与 citations 列表顺序一致，点击出处可打开对应材料。

## Non-goals

- 不做流式输出与长期对话记忆（第 5 课内容）；问答仅附最近若干轮对话便于连续
  追问。
- 不做重排序（交叉编码器）、自定义分数加权、按班拆分 Qdrant 集合等进阶优化。
- 不引入 LangChain / LlamaIndex / Dify 等编排框架，检索链路由本项目代码自行
  完成，便于对照规约逐条验收。
- 不改变迭代 1 的登录、角色校验、上传与班级隔离行为：上传仍仅限教师，本变更
  只在其后追加检索与问答能力，列表页 `LIKE` 筛选保持不变。
- 不支持多嵌入模型并存：全系统同一时刻只使用一个嵌入模型与一个向量集合，更换
  模型必须对全部材料重建索引。

## Impact

- **代码**：新增 `app/chunking.py`（切分）、`app/embedding.py`（嵌入适配）、
  `app/vector_store.py`（Qdrant REST 适配）、`app/retrieval.py`（检索与问答
  编排）；`app/server.py` 新增 `/api/search`、`/api/ask`、
  `/api/materials/{id}/reindex` 路由，并扩展上传接口接收可选策略字段；
  `web/` 新增检索与问答界面。
- **数据**：SQLite 新增表 `knowledge_chunks(id, material_id, class_id,
  chunk_index, chunk_text, char_start, char_end, embed_status)`，启动时
  `CREATE TABLE IF NOT EXISTS` 自动迁移；`materials` 与 `knowledge_entries`
  结构不变。
- **部署**：`docker-compose.yml` 新增 `qdrant` 服务（含数据卷，端口不映射到
  宿主机）；`.env` 新增 `QDRANT_URL`、`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、
  `EMBEDDING_MODEL`、`EMBEDDING_DIM`、`CHAT_BASE_URL`、`CHAT_API_KEY`、
  `CHAT_MODEL` 等配置；嵌入与对话配置缺失时对应功能降级，不阻塞启动。
- **种子数据**：`scripts/init_db.py` 为种子材料按默认 auto 策略补齐切片；配置
  了嵌入网关时同时补齐向量索引。
- **规约**：新增 capability `knowledge-retrieval`，见本变更
  `specs/knowledge-retrieval/spec.md`。
