# Spec Delta: knowledge-retrieval（班级限定检索与可溯源问答）

## ADDED Requirements

### Requirement: R1 材料切分与切片入库

材料上传成功后，系统 MUST 将 `knowledge_entries.body_text` 切分为切片写入
`knowledge_chunks`，每条切片 MUST 记录切片序号与在待切分文本中的字符区间。
系统 MUST 支持三种切分策略：`auto`（默认，最大 800 字、重叠 80 字，优先在
空行、换行、句号处断开）、`custom`（自定义分隔符，最大长度限 100–2000 字、
重叠比例限 0%–50%，无断点处按最大长度强制截断，可选移除 URL/邮箱与折叠
连续空白）、`hierarchy`（按 Markdown `#`/`##`/`###` 分章，标题保留在该章
切片内，超长章节再按 auto 规则细切）。未指定策略时 MUST 按 `auto` 处理；
`auto` 模式下请求另行提交的长度与预处理参数 MUST 忽略。切分 MUST 只读正文，
不得改写 `knowledge_entries.body_text` 与原始文件；`custom` 启用预处理时，
字符区间 MUST 标注为相对预处理后文本。

#### Scenario: R1.1 默认自动切分

- **WHEN** 教师上传约 2000 字的 .md 材料且未指定策略
- **THEN** 返回 201，切片按 auto（800/80）生成，相邻切片存在重叠，
  `chunk_index` 从 0 连续递增
- **AND** `knowledge_entries.body_text` 与上传原文逐字一致

#### Scenario: R1.2 custom 参数校验

- **WHEN** 教师以 `max_len=50` 或 `overlap=60%` 上传
- **THEN** 返回 422，不产生任何切片记录

#### Scenario: R1.3 hierarchy 保留标题

- **WHEN** 教师上传含 `##` 二级标题的 Markdown 材料并选择 hierarchy
- **THEN** 每章的首个切片以该章标题开头

#### Scenario: R1.4 预处理不改写原文

- **WHEN** custom 策略开启"折叠空白"后上传
- **THEN** `knowledge_entries.body_text` 仍保留原始换行
- **AND** 切片 `char_start`/`char_end` 相对预处理后文本计算

### Requirement: R2 向量嵌入与双库存储

系统 MUST 经 OpenAI `/embeddings` 兼容网关在服务端生成切片与问句向量，网关
密钥 MUST 仅存于服务端配置，MUST NOT 下发浏览器或出现在任何接口响应中。
向量 MUST 存入 Qdrant 集合 `campusclaw_chunks`（余弦距离），点主键 MUST 与
`knowledge_chunks.id` 一致，payload MUST 只含 class_id、material_id、
entry_id、chunk_id、chunk_index 且 MUST NOT 含切片正文；切片正文 MUST 仅存
于 SQLite，并在检索命中后回表读取。嵌入返回维度 MUST 与 `EMBEDDING_DIM` 及
集合维度一致，不一致 MUST 拒绝写入。单个切片嵌入失败时，系统 MUST 保留材料
记录、将该切片标记为 failed，且 MUST NOT 写入不完整向量数据；仅
`embed_status='ready'` 的切片参与检索。

#### Scenario: R2.1 点主键与 payload 形态

- **WHEN** 教师上传材料且嵌入成功
- **THEN** Qdrant 中每个点的 id 等于对应 `knowledge_chunks.id`
- **AND** payload 恰为五个标识字段，不含任何正文文本

#### Scenario: R2.2 嵌入失败保留材料

- **WHEN** 嵌入网关不可用时教师上传材料
- **THEN** 上传仍返回 201，`materials` 与 `knowledge_entries` 记录保留
- **AND** 对应切片 `embed_status='failed'`，Qdrant 中无该材料的点

#### Scenario: R2.3 密钥与向量不外泄

- **WHEN** 检查全部接口响应与页面 DOM
- **THEN** 不出现网关密钥，也不出现向量分量数值

### Requirement: R3 重建索引

系统 MUST 提供教师对本班材料重建索引的能力：先删除该材料全部旧切片与旧向量
点，再按本次请求的策略重新执行切分、嵌入与写入。上传与重建 MUST 使用同一组
策略参数校验。已入库材料 MUST NOT 自动重新切分，更换策略 MUST 通过显式重建
完成。非教师角色调用 MUST 返回 403，跨班材料 MUST 按不存在处理返回 404。

#### Scenario: R3.1 教师更换策略重建

- **WHEN** 教师对已按 auto 入库的材料调用 reindex 并指定 hierarchy
- **THEN** 旧切片行与旧 Qdrant 点全部删除，新切片按 hierarchy 生成
- **AND** Qdrant 中该 material_id 的点数与新切片数一致，无旧点残留

#### Scenario: R3.2 重建的角色与班级校验

- **WHEN** 学生调用 reindex，或教师对其他班的材料调用 reindex
- **THEN** 分别返回 403 与 404

### Requirement: R4 检索接口与三种模式

系统 MUST 提供 `GET /api/search?q=<查询>&mode=<keyword|vector|hybrid>`
（默认 hybrid），学生与教师均可检索本班材料。keyword 模式 MUST 仅查切片正文
（词元匹配计数、按相关度降序），MUST NOT 调用嵌入、MUST NOT 访问向量库；
vector 模式 MUST 将问句嵌入后按余弦检索、丢弃相似度低于 0.35 的候选并按余弦
降序；hybrid 模式 MUST 并行执行两路，各路先独立按绝对分数过滤，再按名次执行
RRF（k=60）融合，MUST NOT 直接相加分数，缺席的一路不贡献分数。查询为空或仅
空白 MUST 返回 400，未登录 MUST 返回同形 401。每条命中 MUST 包含材料标题、
切片序号、字符区间、摘录与该模式下的排序分数。

#### Scenario: R4.1 keyword 词面命中

- **WHEN** 以材料原文中出现的词语检索
- **THEN** 命中包含该词的切片并按相关度降序排列
- **AND** 全程无嵌入调用、无 Qdrant 访问

#### Scenario: R4.2 vector 语义命中

- **WHEN** 以与切片表述不同但语义相同的问句检索
- **THEN** 命中该切片且余弦相似度 ≥ 0.35

#### Scenario: R4.3 hybrid 融合排序

- **WHEN** 构造使切片 X 被两路同时命中、切片 Y 仅被一路命中且两路名次相近
- **THEN** X 的融合名次高于 Y

#### Scenario: R4.4 空查询

- **WHEN** `q` 为空字符串或缺失
- **THEN** 返回 400

### Requirement: R5 检索侧班级隔离

检索的班级标识 MUST 仅取自登录会话；查询串、请求体或请求头中另行携带的班级
编号 MUST 在解析后丢弃。关键字与向量两条路径 MUST 均按会话班级过滤；向量命中
后回表取正文 MUST 再次以同一班级值约束。跨班检索对外表现 MUST 为 200 与空
命中列表（问答为「资料中未找到相关内容」），MUST NOT 以 403/404 暗示其他班
存在相应内容。

#### Scenario: R5.1 请求携带班级编号无效

- **WHEN** A 班用户在请求体中提交 B 班的 class_id 后检索
- **THEN** 过滤仍使用会话中的 A 班，结果不含 B 班切片

#### Scenario: R5.2 跨班词检索返回空

- **WHEN** 以仅 B 班材料含有的词语以 A 班身份检索
- **THEN** 返回 200 且 `hits` 为空

#### Scenario: R5.3 回表复核兜底

- **WHEN** 向量库中被人工写入其他班的越权点
- **THEN** 回表 SQL 的班级约束仍排除该点的正文，检索结果不出现他班内容

### Requirement: R6 内容溯源

检索结果与问答引用 MUST 均包含材料标题、切片序号、字符区间、摘录四要素，并
提供打开对应材料的途径。摘录 MUST 由服务端截取自 SQLite 切片正文的连续子串，
MUST NOT 取自向量库，MUST NOT 接受客户端提交的摘录或出处。问答回答中的
[1]、[2] 标注顺序 MUST 与 citations 列表顺序一致，页面出处列表顺序 MUST 与
之一致；点击出处 MUST 打开对应材料，跨班材料标识按迭代 1 规则返回 404。

#### Scenario: R6.1 四要素齐全

- **WHEN** 任一检索命中返回
- **THEN** hit 含材料标题、切片序号、字符区间、摘录及 material_id

#### Scenario: R6.2 摘录可回溯原文

- **WHEN** 按返回的字符区间从切片正文截取文本
- **THEN** 截取结果与摘录一致

#### Scenario: R6.3 出处可打开且跨班不可见

- **WHEN** 用户点击出处打开材料，或以他班 material_id 请求材料详情
- **THEN** 前者打开本班材料，后者返回 404

### Requirement: R7 无依据不凑数

当班级过滤后没有任何候选切片时，系统 MUST 返回 200 与固定文案「资料中未找到
相关内容」且 citations 为空，MUST NOT 调用对话网关，MUST NOT 以相关度明显
偏低的切片凑数。

#### Scenario: R7.1 语料外问题

- **WHEN** 用户提问天气、比分等语料中不可能出现的问题
- **THEN** 返回 200、固定文案、citations 为空
- **AND** 对话网关无任何调用

#### Scenario: R7.2 拒绝低分凑数

- **WHEN** 仅存在余弦相似度低于 0.35 的候选切片
- **THEN** 视为无命中，按 R7.1 处理

### Requirement: R8 问答生成与边界

系统 MUST 提供 `POST /api/ask`：先以用户最新一句提问执行本班混合检索并取前
4 条切片；仅当存在命中切片时才调用对话网关生成简短回答，回答 MUST 以 [n]
标注依据来源。上下文 MUST 只包含材料标题、切片序号、切片正文与用户本轮提问
（可附最近若干轮对话），MUST NOT 包含向量分量或其他班级切片；客户端提交的
system 消息 MUST 丢弃，system 提示由服务端写入；对话生成组件 MUST NOT 自行
访问数据库或向量库检索其他材料。本迭代 MUST NOT 实现流式输出；对话网关不可用
时 MUST 返回同形 503 且不编造回答。

#### Scenario: R8.1 有据生成

- **WHEN** 用户提问且混合检索命中 ≥2 条切片
- **THEN** 回答内容依据所提供切片生成并含 [1]、[2] 标注

#### Scenario: R8.2 system 消息丢弃

- **WHEN** 请求体携带客户端构造的 system 消息
- **THEN** 服务端忽略该消息，网关收到的 system 提示与不携带时一致

#### Scenario: R8.3 网关故障不编造

- **WHEN** 存在命中切片但对话网关不可用
- **THEN** 返回 503 同形错误，不返回任何编造的回答

### Requirement: R9 依赖降级与暴露面

向量库或嵌入网关不可用时：keyword 模式 MUST 仍可用；vector 与 hybrid 模式
MUST 返回同形 503 且 MUST NOT 编造分数或结果。Qdrant MUST NOT 对客户端暴露
端口，班级过滤与回表查询 MUST 全部保留在服务端。

#### Scenario: R9.1 停用 Qdrant 后关键字可用

- **WHEN** Qdrant 服务停止后分别以 keyword 与 vector / hybrid 检索
- **THEN** keyword 返回 200 正常结果，vector 与 hybrid 均返回 503

#### Scenario: R9.2 向量库端口不暴露

- **WHEN** 从宿主机直连 Qdrant 端口
- **THEN** 连接被拒绝（Compose 未映射该端口）
