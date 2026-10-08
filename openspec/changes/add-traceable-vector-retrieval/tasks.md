# Tasks: 可追溯知识库检索（迭代 2）

> 每个 task 一轮：实现 → 执行 verify → 勾选 `- [x]`。verify 均为可执行命令 /
> 接口调用；涉及嵌入与对话网关的场景需先在 `.env` 配置课程网关。

## §1 切分器与切片入库

- [x] 1.1 `knowledge_chunks` 建表与索引（启动时 `CREATE TABLE IF NOT EXISTS`，
      `embed_status` CHECK('ready','failed')）— verify: 启动后用 sqlite3 查询
      表结构，含 CHECK 约束与 `idx_chunks_class` / `idx_chunks_material`
- [x] 1.2 auto 策略：800 字窗口、80 字重叠、优先空行 / 换行 / 句号断开；
      附带的其他参数忽略 — verify: 上传约 2000 字讲义（不带 strategy）→ 201，
      切片数 ≥2、相邻切片重叠内容一致、`chunk_index` 从 0 递增
- [x] 1.3 custom 策略：分隔符切分，max_len 限 100–2000、overlap 限 0%–50%，
      无断点强制截断，参数越界 422 — verify: `max_len=50` 上传 → 422；
      `max_len=300&sep=句号` → 每片 ≤300 字
- [x] 1.4 hierarchy 策略：按 `#`/`##`/`###` 分章，标题保留在章内，超长章节
      再按 auto 细切 — verify: 上传含二级标题的 .md → 每章首切片以标题开头
- [x] 1.5 预处理不改写原文：custom 开启"移除 URL/邮箱、折叠空白"后
      `knowledge_entries.body_text` 仍为上传原文，切片区间相对预处理后文本
      — verify: 上传后比对 body_text 与原文件逐字一致
- [x] 1.6 上传默认策略为 auto，响应含切片统计 — verify: 不带 strategy 上传
      → 响应含 `chunks_total` 与 failed 计数

## §2 嵌入与向量入库

- [ ] 2.1 嵌入适配器：OpenAI `/embeddings` 兼容，仅服务端调用，维度三方校验
      （响应 = EMBEDDING_DIM = 集合维度）— verify: 配置错误 `EMBEDDING_DIM`
      → 入库报错且不写向量
- [ ] 2.2 Qdrant 适配器（urllib REST）：启动幂等建集合 `campusclaw_chunks`
      （余弦）— verify: 容器内 `GET /collections/campusclaw_chunks` 维度与
      `EMBEDDING_DIM` 一致
- [ ] 2.3 点主键 = `knowledge_chunks.id`，payload 仅 class_id / material_id /
      entry_id / chunk_id / chunk_index，无正文 — verify: 任取一点核对
      payload 字段列表与 SQLite 对应行
- [ ] 2.4 嵌入失败语义：切片 failed、材料记录保留、不写不完整向量 — verify:
      停网关上传 → 201，切片 `embed_status='failed'`，Qdrant 无该
      material_id 的点
- [ ] 2.5 种子材料启动补索引（auto，配置网关时含向量）— verify: 重新初始化后
      种子材料 ready 切片数 > 0

## §3 检索接口与三种模式

- [x] 3.1 `GET /api/search` 鉴权与参数校验 — verify: 无 Cookie → 同形 401；
      `q` 为空 / 缺失 → 400
- [x] 3.2 keyword 路径：词元计数评分降序，只取本班 ready 切片，不调嵌入、
      不访 Qdrant — verify: 用材料原词检索命中该切片；网关访问日志零
      embeddings 调用
- [ ] 3.3 vector 路径：问句嵌入后带 class_id 过滤检索，余弦 <0.35 丢弃，
      按余弦降序 — verify: 同义改写（词面不同）检索命中同一切片且 score ≥0.35
- [x] 3.4 hybrid 路径：两路各自过滤后按名次 RRF（k=60）融合，缺席一路不贡献
      分数 — verify: 构造切片 X 双路命中、切片 Y 单路命中且名次相近 → X 排前
- [x] 3.5 hit 携带四要素与排序分数 — verify: 响应字段与 SQLite
      `chunk_text` 连续截取比对一致

## §4 检索侧班级隔离

- [x] 4.1 班级仅取会话：请求携带的班级编号解析后丢弃 — verify: A 班会话在
      query / body 填 B 班 class_id → 结果仍限 A 班，无 B 班切片
- [x] 4.2 跨班词检索 200 + 空 hits — verify: 用仅 B 班材料含有的词以 A 班身份
      检索 → 200、`hits=[]`，非 403/404
- [ ] 4.3 回表复核：向量命中后回表 SQL 再次约束班级 — verify: 向 Qdrant 手工
      upsert 伪造他班点 → 检索结果不出现其正文
- [x] 4.4 学生可检索本班材料 — verify: 学全会话检索 → 200 正常返回

## §5 问答与溯源

- [ ] 5.1 有命中时调用对话网关，回答含 [1][2] 标注且与 citations 顺序一致
      — verify: 提问命中 ≥2 切片 → 标注序号与 citations 数组顺序逐一对应
- [ ] 5.2 无命中不调模型：200 固定文案「资料中未找到相关内容」+ 空 citations
      — verify: 提问天气 / 比分 → 固定文案；网关日志零 chat 调用
- [x] 5.3 客户端 system 消息丢弃，system 提示由服务端写入 — verify: 请求体
      携带 system 字段 → 网关收到的 messages 与不携带时一致
- [ ] 5.4 上下文边界：仅材料标题、切片序号、切片正文与本轮提问（附最近若干
      轮），无向量分量、无他班切片 — verify: 抓取网关请求体核对字段
- [x] 5.5 对话网关不可用 → 同形 503，不编造回答 — verify: 停网关提问 → 503

## §6 重建索引与依赖降级

- [ ] 6.1 教师重建：先删旧点旧行再按本次策略重切重嵌，无旧点残留 — verify:
      auto 入库后 reindex hierarchy → 切片结构变化，Qdrant 该 material_id
      点数与新切片数一致
- [x] 6.2 重建权限：非教师 403、跨班材料 404 — verify: 学生调用 reindex →
      403；教师对 B 班材料 → 404
- [ ] 6.3 停 Qdrant：keyword 仍可用，vector / hybrid 同形 503 不编造分数
      — verify: `docker stop qdrant` → keyword 200，vector 与 hybrid 均 503
- [ ] 6.4 Qdrant 不对客户端暴露 — verify: 宿主机 `curl localhost:6333` 连接
      被拒绝；compose 无 6333 端口映射

## §7 前端

- [ ] 7.1 检索页：三模式切换、结果卡片展示四要素、点击出处打开材料 — verify:
      `scripts/verify.py` 新增浏览器场景通过
- [ ] 7.2 问答页：回答内 [n] 与出处列表顺序一致，出处可点击 — verify: 同上
- [x] 7.3 页面与接口响应不出现向量分量与网关密钥 — verify: 全接口响应与页面
      DOM 关键词扫描无 `embedding` / `vector` 数值 / key 字段

## §8 发布验收

- [ ] 8.1 Compose 全链路（web + qdrant）— verify: `docker compose up` →
      `/health` 200，检索与问答可完整走通
- [x] 8.2 `scripts/verify.py` 迭代 1 的 30 项场景回归 + 新增场景全过 — verify:
      运行脚本输出全绿
- [x] 8.3 README 更新迭代 2 说明（检索、溯源、降级、配置）— verify: README
      含对应章节与命令
