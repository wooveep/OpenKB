# OpenKB 本地知识图谱与原生桌面重构设计

> **2026-08-14 桌面核心范围更新：** 桌面首版的功能、体验、存储和兼容性边界已由
> `2026-08-14-openkb-desktop-core-upgrade-design.md` 及 `docs/adr/0001`–`0022`
> 进一步确认。两者冲突时，以该增量规格和 ADR 为准。

- 状态：已确认，分阶段实施中（R0-C2 单一实施计划已生成，待实施）
- 日期：2026-08-08
- 最近路线更新：2026-08-15
- 目标平台：Windows 10/11 64 位
- 产品形态：单机、单用户、本地优先

## 1. 决策摘要

OpenKB 将从“Markdown 文件树为事实来源、长事务串行导入、Web 工作台”演进为以下形态：

1. SQLite 是唯一业务权威；`raw/` 保存唯一完整原文，内容寻址存储（CAS）只保存大型中间与派生产物；Markdown 是可重建、可导出、可阅读的知识视图。
2. 导入流程改为可恢复的阶段 DAG。解析完成并形成证据层后，PageIndex、全文索引、去重和图谱抽取并行执行。
3. 在现有树状组织和 wikilink 关系之上引入正式知识图谱：节点、受控类型边、置信度、来源、证据、版本和审核状态；首版只提供证据锚定的局部图谱检索。
4. LLM 在同一导入任务中辅助抽取图谱，但不持有数据库事务，也不能仅凭模型自报置信度发布事实。
5. 新图谱增强现有 PageIndex 和 Wiki 检索，不在质量门禁通过前替代它们。
6. 使用 Tauri 2 + React/Vite 构建 Desktop Shell，并由它监管承载领域能力的 Python Engine；
   Rust 只负责原生壳层、IPC、进程生命周期和打包，不承载 OpenKB 业务规则。
7. Desktop Workbench 验收后删除旧 CLI、React WebUI 及其专用 REST/SSE 接口；旧知识库迁移和
   独立维护入口后续另行规划。
8. 采用分级发布；任一数据完整性、迁移或问答质量门禁失败，都阻止进入下一等级。

当前 R0 已进一步拆分并执行：R0-A 质量基线、R0-B 候选权威存储和 R0-C1 Stage
Runtime Kernel 已完成自动化开发门禁；下一阶段是一个连续计划内的 R0-C2a 全局
Model Gateway 与 R0-C2b Profile 管理。R0-C2b 只迁出并统一管理现有 Prompt、默认
`wiki/AGENTS.md` 和内置 SKILL 资源，禁止修改其内容或运行时语义。R0-C2 的权威子规格
是 `docs/superpowers/specs/2026-08-13-r0-c2-model-gateway-profile-management-design.md`。

本设计基于审计快照借鉴
[Neo4j Labs LLM Graph Builder `5ff7af3`](https://github.com/neo4j-labs/llm-graph-builder/tree/5ff7af3e9bb9226e1bbecd02f70f8d98697727a7)
的 schema 约束、实体/关系候选抽取、后处理和多通道 GraphRAG 思路，以及
[RAGFlow `554fb11`](https://github.com/infiniflow/ragflow/tree/554fb1133ac3861732235ad9c377eb5e0a770665)
的格式专用解析、Document IR、PDF OCR/版面/表格
流水线和解析/分块分离方式，但不整体引入任一项目。OpenKB 不采用 Neo4j、Cypher、APOC、
GDS、RAGFlow 服务架构、Embedding 模型、向量索引或完整依赖集合；所有能力在现有 Python、
SQLite、PageIndex 和知识页基础上原生实现。任何代码或模型复用和新增依赖均须单独完成许可证、
维护性和供应链审查，并精确锁定版本。

## 2. 背景与问题

2026-08-08 的导入日志显示，一批 69 份文件在记录到第 33 份时，前 32 份虽显示完成，仍存在多类非致命或降级问题：

- LiteLLM 日志工作线程多次超时；
- PageIndex 返回无效 JSON 或无法稳定解析 TOC `found` 字段；
- 部分 PageTree 低精度结果仍被报告为成功；
- 概念和实体更新占据主要模型调用及服务时间；
- 当前导入锁覆盖转换、PageIndex、概念和实体编译等长耗时操作；
- 失败通常导致整份文档重试，阶段级进度和恢复能力不足；
- 页面级重复内容仍可能进入后续高成本处理；
- 当前图结构主要由 Markdown wikilink 推导，只能表达无类型关系。

这些问题共同造成导入耗时长、状态失真、失败恢复成本高、证据与关系难以审计。重构必须解决这些问题，同时保住当前 OpenKB 的组织方式和问答质量。

## 3. 目标与非目标

### 3.1 目标

- 支持约 1,000 份文档、100,000 页、50,000 个正式图节点和 250,000 条正式边的中等规模知识库。
- 让导入阶段可观察、可暂停、可取消、可有限重试、可从检查点恢复。
- 建立可追溯的证据层和有审核语义的知识图谱。
- 让 PageIndex、全文检索、Wiki 视图和图谱协同回答问题。
- 提供无需浏览器和本地 Web 服务的 Windows 原生 GUI。
- 保持离线业务数据可用；除用户明确配置的模型服务和 URL 导入外，不隐式外联。

### 3.2 非目标

- 不支持多用户、远程协作、权限体系或服务端部署。
- 不引入 Neo4j、图数据库服务器或任意 Cypher 查询。
- 不在首版提供任何全图、局部图或社区浏览界面；知识图谱只作为内部检索通道。
- 不使用 Embedding 模型、向量索引或向量数据库；语义能力来自查询规划、PageTree、知识页、
  词法召回和证据化图谱。
- 不把 LLM 生成内容直接视为已证实事实。
- 不让 Markdown 反向覆盖 SQLite。
- 不要求本地 GPU；本地模型只是 Model Gateway 的一种可选后端。
- 不在核心重构中加入与数据流、知识图谱、问答和桌面操作无关的功能。

## 4. 术语与业务状态

### 4.1 “正式”与“发布”

“正式知识”指已经通过 Publisher 进入 `PUBLISHED` 状态，并可被默认检索和问答使用的节点、主张或关系。候选、待审核、冲突、拒绝和历史修订均可审计，但不属于默认知识视图。

### 4.2 就绪里程碑

- `REGISTERED`：原始资产已完整写入 `raw/`，其散列、长度、文档版本和任务记录已提交。
- `SEARCH_READY`：证据层和基础 FTS5 `SearchGeneration` 可用；文档级或语料级 PageIndex PageTree 作为可选检索能力单独标记就绪或降级。
- `KNOWLEDGE_READY`：本次允许发布的节点、主张和关系已完成协调与发布。

后续派生任务失败不得撤销上一代已经发布且仍有效的里程碑。

### 4.3 任务状态

`Job` 和 `StageRun` 使用明确状态机：

`PENDING -> RUNNING -> {SUCCEEDED, DEGRADED, RETRY_WAIT, FAILED, CANCELLED}`

`RETRY_WAIT` 可回到 `RUNNING`。恢复、重试和取消都必须以状态机命令执行，不能直接改状态字段。部分成功不得显示为完整成功。

## 5. 质量不倒退原则

SQLite 权威化不等于放弃当前 Wiki 组织。新系统必须持续物化与当前语义等价的摘要、概念、实体、索引和 PageIndex 树视图，并保留四条检索通道：

1. PageIndex PageTree 推理；
2. SQLite FTS5 的 Evidence/Claim 检索；
3. 物化 Wiki 的摘要、概念、实体和索引；
4. 正式知识图谱的节点、关系和证据锚定的 1–2 跳局部路径。

四条通道均为 vectorless，不存在第五条 Embedding/vector 通道。查询时可以调用 LLM 生成受控
Retrieval Plan 和对小规模候选重排，但任何规划或重排失败都必须回退到确定性查询词、通道排名
和 RRF，不能使基线召回不可用。

图谱是附加通道。初始发布时，现有基线通道保持原有独立召回预算 `K_base`，图谱使用额外预算 `K_graph`，不得通过共享上限挤掉基线候选。候选可使用基于排名的融合去重，但最终上下文必须保留配置的基线最低配额。

每个回答必须能追溯到 `EvidenceRef`。图谱路径只有在涉及的边均已发布时才能进入默认回答；推断关系必须显式标记，不得伪装为原文事实。PageTree 降级时回退到 Evidence/FTS/Wiki，并在界面显示能力降级。

固定回归集使用同一模型、提示、参数、重复次数和语料快照进行对照。正式门禁要求：

- Recall@K 不低于旧版；
- 每个问题的关键证据不得丢失；
- 聚合正确性、完整性和引用质量不低于旧版；
- 正式主张和关系的证据可打开率为 100%；
- 无证据断言比例不得上升；
- 降级结果不得被报告为正常结果。

图谱增强由知识库级特性开关控制。只有通过回归门禁后，才可成为该知识库的默认问答通道。

## 6. 总体架构

```text
React Workbench <──typed Desktop Bridge──> Tauri Desktop Shell
                                                |
                                      private stdio JSON-RPC
                                                v
                                      Python Application Service
                                                |
                              Job Manager / Stage DAG / Model Gateway
                                    |           |           |
                              parser workers  graph workers  DB commands
                                                |
                                                v
                                       Dedicated DB Writer
                                        |       |       |
                                     SQLite    CAS    View Materializer
                                  (authority)       (Markdown/derived)
```

### 6.1 进程与线程边界

- Tauri Desktop Shell 负责窗口、系统托盘、单实例、文件对话框、Desktop Bridge 和 Python
  Engine 生命周期；React 只处理工作台视图与临时 UI 状态。
- Python Engine 的后台 asyncio 循环负责 Job 调度、模型请求和事件汇聚。
- CPU 密集型转换、解析和图计算使用进程工作者；Windows 固定采用 `spawn`，传递可序列化命令和结果。
- 所有权威写入经专用 DB Writer 串行化，使用短事务；读取使用独立只读连接。
- Windows Job Object 管理 Desktop Shell、Python Engine 和 worker 进程树；工作者不能持有 UI
  对象、长数据库事务或直接修改 Markdown 知识视图。

### 6.2 Python 包边界

- `domain`：实体、值对象、状态机、谓词和发布规则；不依赖 GUI、SQLite 或供应商 SDK。
- `storage`：SQLite 仓储、迁移、CAS、备份和 generation 指针。
- `pipeline`：Stage DAG、调度、幂等、租约、重试、取消和事件。
- `knowledge`：证据、抽取候选、身份协调、证据化图谱和 Publisher；社区属于后续增强包。
- `retrieval`：PageIndex、FTS、Wiki、图谱召回、融合和引用构造。
- `application`：面向 Desktop Bridge 的命令、查询和用例编排。
- `model`：类型化 Model Gateway、deadline/retry、响应验证、Agents SDK bridge 和调用契约。
- `profiles`：Prompt、Agent、Wiki Schema 和 Skill 的不可变资源、注册表、固定、升级与回退。
- `desktop`：Tauri Desktop Shell、React Workbench、Desktop Bridge、任务中心和 Windows 集成；
  生产包加载静态前端，不启动本地 Web 服务。
- `adapters`：格式专用 Parser Adapter、唯一 LiteLLM transport、本地模型、URL、文件系统和
  旧版迁移适配器。

每个包通过类型化接口通信；供应商返回值必须在适配器边界完成解析和验证。任何模块仍须遵守少于 800 行的项目约束。

## 7. 权威数据与派生数据

### 7.1 SQLite 权威数据

下列信息只能以 SQLite 为业务权威：

- `Asset`、`SourceDocument`、`DocumentVersion`、`EvidenceRef`；
- `KnowledgeNode`（`ENTITY`、`CONCEPT`、`CLAIM`）、别名和类型；
- `TypedEdge`、边证据、修订和发布状态；
- `IdentityCandidate`、审核队列和审核决定；
- `PublishEvent`、当前发布 generation 和用户覆盖；
- `Job`、`StageRun`、模型尝试和恢复租约；
- 导入清单、配置快照和迁移日志。

SQLite 使用外键、唯一约束和检查约束维护核心不变量。应用启动时校验 schema 版本；迁移只能前进，并在迁移前创建可验证备份。

### 7.2 Raw Asset 与 CAS

每份不同原始内容的完整字节只在 `raw/` 保存一次，并由 SQLite 记录 SHA-256、长度和生命周期。
CAS 不保存第二份完整文档。CAS 以内容的 SHA-256 为键，只保存：

- 转换后的稳定中间表示；
- 大型 PageTree、图像和可重新计算的批量派生产物；
- 导入或迁移快照。

写入过程为“临时文件 -> 散列与长度校验 -> 原子重命名 -> SQLite 引用提交”。相同内容只能产生一个 CAS 对象；删除采用引用计数和延迟回收，不在业务事务中直接擦除文件。

### 7.3 派生数据

以下数据可从 SQLite/CAS 重建，不是业务权威：

- FTS5 索引；
- PageTree 查询缓存；
- 后续可选的社区划分和社区摘要缓存；
- Markdown Wiki、摘要、概念、实体和索引视图；
- 缩略图、临时 IR 和统计缓存。

派生输出带 `generation_id`。新 generation 全部校验成功后才切换当前指针，避免读取半生成数据。Markdown 导出不被文件监听器反向导入，除非用户显式执行受控“作为新来源导入”。

## 8. 数据流与阶段 DAG

### 8.1 阶段

```text
S0 发现与 Preflight
      │
      ▼
S1 Asset + 不可变 DocumentVersion
      │  D0：原始文件 SHA-256 精确去重
      └───────────────────────────────────────► REGISTERED
      │
      ▼
S2 DocumentIR
      │  heading/paragraph/list/code/table/figure 与来源坐标规范化
      │  D1：规范化正文精确去重
      ▼
S3 Evidence
      │  D2：页面与证据片段去重
      │
      ├─► S4a FTS5/BM25 ─────────────► 基础 SearchGeneration ──► SEARCH_READY
      │                                                       门槛：Evidence + FTS
      ├─ S4b 文档 PageTree ─┐
      ├─ S4c 语料 PageTree ─┴─────────► 后继 SearchGeneration
      │                                 可选能力；失败只标记 DEGRADED
      ├─ S4d 确定性图谱候选 ─────────┐
      ├─ S4e LLM 图谱/Claim 候选 ────┤
      └─ S4f D3 近重复候选 ──────────┤
                                      ▼
                            S5 批次级合并与冲突检测
                                      │
                                      ▼
                          S6 Publisher 原子正式发布
                                      │
                                      ▼
                           KnowledgeGeneration
                                      │
                               KNOWLEDGE_READY
                                      │
                                      ▼
                            S7 派生视图生成
                    ┌─────────────────┴────────────────┐
                    ▼                                  ▼
             Markdown Wiki/索引                 缩略图/查询缓存
               ViewGeneration                    Derived Cache
```

- S0 发现文件、URL 或目录项，执行 Preflight 并生成确定性导入清单。
- S1 将原始内容注册为不可变文档版本并执行 D0；不在长锁内执行模型或转换工作。
- S2 由格式专用 Parser Adapter 生成统一 Document IR，保留块顺序、标题层级、表格、图片和
  page/slide/sheet/cell/bbox 等来源坐标，同时执行 D1。Markdown 只是从 IR 物化的阅读视图。
- TXT/Markdown、DOCX、XLS/XLSX、PPTX 使用对应 Python 解析库；PDF 使用 PyMuPDF 快速路径和
  随包 DeepDoc ONNX 增强路径；旧 DOC/PPT 使用随包 python-tika、私有 Tika Server JAR 与
  精简 Java Runtime 进行低保真文本兼容读取。禁止引入 LibreOffice 或首次使用时联网下载资源。
- S3 形成稳定 `EvidenceRef` 并执行 D2；后续所有主张和关系必须引用证据层。
- S4 分支并行运行。FTS5/BM25 先形成基础 `SearchGeneration`；只要 Evidence 与 FTS 可用即可达到 `SEARCH_READY`。文档 PageTree 和语料 PageTree 验证通过后发布包含对应能力的后继 `SearchGeneration`，失败必须显式标记降级，不能阻断或撤销基础搜索。
- 确定性图谱和 LLM 图谱/Claim 抽取均直接依赖 Evidence，不从 SearchGeneration 或
  Markdown 派生权威事实。LLM 图谱解析与文档导入在同一 Job 中同步推进，但在证据层之后物理异步执行。
- S4f 只负责不能自动删除内容的 D3 语义近似候选；它进入协调流程，不属于 SearchGeneration。
- S5 合并重复候选、身份候选、冲突和证据支持。
- S6 按发布规则生成不可变修订和 `PublishEvent`，并只在完整验证后原子切换
  `KnowledgeGeneration`。
- S7 从已发布权威数据计算可丢弃的 `ViewGeneration` 和缓存；Markdown 不参与反向发布。

### 8.2 幂等与恢复

每个 StageRun 的幂等键至少包含：文档版本散列、阶段名、阶段 schema 版本、相关配置散列、提示版本和模型配置标识。相同幂等键的成功结果可复用。

任务使用租约和心跳。应用异常退出后，过期的 `RUNNING` 任务先转为可恢复状态，再从最近已提交阶段继续。取消是协作式的：停止派发新工作，等待当前原子步骤结束，保留已验证结果，不发布未完成 generation。

### 8.3 去重

去重分四级执行：

- D0：原始资产散列完全相同；
- D1：规范化正文散列完全相同；
- D2：页面或证据片段重复；
- D3：语义近似候选，仅用于提示或审核，不自动删除。

D0–D2 在进入昂贵模型阶段前尽早复用结果。重复证据不能在置信度聚合中被视为独立支持。

## 9. Model Gateway 与有限重试

所有 OpenKB 发起的云端和本地模型请求通过统一 Model Gateway 调用。这里的“统一”是
全局逻辑入口，不是保存可变 credentials/config 的进程级单例。Compiler 的直接
LiteLLM 调用与 Query、Chat、Linter、Skill、Deck、Evaluation 的 Agents SDK 调用都
必须迁移；只有严格限定的 LiteLLM transport 适配器允许直接调用 LiteLLM。

每次逻辑调用及其每个物理供应商 Attempt 进入 schema v4 SQLite ledger，记录任务或
operation、可空 Stage/文档/Agent 关系、请求散列、供应商请求 ID、Model/Prompt/Agent/
Skill/Wiki Schema Profile 身份、耗时、令牌、结果状态和经过脱敏的错误。密钥、敏感
Header、完整 Prompt、正文、工具输出和完整响应不得写入 SQLite 或日志。模型网络请求
永远在 SQLite 事务之外执行。

默认最多 4 次尝试，即首次调用加 3 次自动重试。首次请求超时默认 20 秒，每次重试增加
10 秒；整个逻辑 Model Call 的 API 响应等待硬截止为 60 秒，并优先于剩余重试次数。仅以下
情况自动重试：

- 连接或读取超时；
- HTTP 408、429；
- HTTP 5xx；
- 可判定为临时性的连接中断。

重试遵守供应商 `Retry-After`，但退避、请求执行和所有 Attempt 的等待总和都不能越过 60 秒
硬截止。鉴权、无效配置、输入/文档格式、响应 schema/格式错误、内容策略拒绝和确定性 4xx
直接失败，不进行自动重试或 repair round。Gateway Attempt、Agent turn 和 Stage retry 使用
不同 ID 与计数器；任何一层都不得暗中重置或乘法放大另一层预算。预算耗尽后，上层按调用
场景进入明确失败、降级或隔离，不得无限循环或把失败报告为成功。

LiteLLM 或其他日志工作线程异常只影响遥测，不改变业务调用的提交结果。遥测写入有界缓冲区，溢出时记录汇总计数而不阻塞业务任务。

R0-C2b 在 Gateway 全路径迁移通过后建立统一 Profile 管理。所有现有静态模型指令、
默认 `wiki/AGENTS.md`、内置 SKILL.md 和 references 的运行时 materialized 字节冻结为
`v1`，只迁移权威存放位置并增加注册、散列、验证、固定、显式升级和回退。R0-C2b
禁止措辞优化、格式化、换行转换或自动覆盖 KB/user custom Profile；内容优化属于未来
独立规格。

## 10. 知识图谱

### 10.1 节点

默认可见的正式知识节点：

- `ENTITY`：人物、组织、地点、产品、系统等可识别对象；
- `CONCEPT`：主题、方法、领域和抽象概念；
- `CLAIM`：可被证据支持或反驳的陈述。

证据节点 `SourceDocument`、`DocumentVersion` 和 `EvidenceRef` 默认隐藏，只在来源追踪和审核界面展示。

节点具有稳定键、规范名称、类型、别名、状态、修订、创建方式和证据。名称相同不能直接合并。无法确定身份时创建 `IdentityCandidate`，由规则、更多证据或用户审核处理。

### 10.2 类型边

V1 内建受控谓词：

`IS_A`、`PART_OF`、`RELATED_TO`、`DEPENDS_ON`、`USES`、`PRODUCES`、`LOCATED_IN`、`CREATED_BY`、`PRECEDES`、`REPLACES`、`SUPPORTS`、`CONTRADICTS`。

谓词注册表规定允许的源/目标类型、方向、是否对称、是否传递、逆谓词、证据要求和发布策略。新谓词必须先审核注册，不能由模型自由写入正式图谱。

每条 `TypedEdge` 至少包含：

- 源、谓词、目标；
- `support_score`（0–1；仅未评分的 `USER` 或 `LEGACY_IMPORT` 记录可为空）；
- 来源方式：`EXTRACTED`、`INFERRED`、`USER` 或 `LEGACY_IMPORT`；
- 一个或多个 `EvidenceRef`；
- 抽取器、模型、提示、schema 和 generation 标识；
- 候选/待审核/发布/拒绝/替代状态；
- 修订、审核决定和冲突信息。

### 10.3 支持度

`support_score` 是发布决策分数，不宣称等同于统计概率。单条证据贡献由抽取器可靠度、证据质量和模型输出质量共同决定。多个相互独立的贡献使用 `1 - ∏(1 - contribution_i)` 聚合；同一文档版本中的重复或高度重叠证据先归组并设贡献上限，防止重复片段虚增支持度。

模型自报置信度只能作为贡献因子，不能独立触发发布。相反谓词或 `CONTRADICTS` 形成单独冲突标记，不通过简单减分隐藏。

### 10.4 分级发布

- 通过 schema、端点和证据验证的确定性关系自动发布。
- LLM 候选只有在谓词已注册、端点已解析、证据可打开、无冲突且 `support_score >= 0.90` 时自动发布。
- `0.60 <= support_score < 0.90`、存在冲突、身份未决或请求新谓词的候选进入审核。
- `support_score < 0.60` 的候选拒绝进入正式图，但保留审计记录。
- 用户显式创建或确认的关系以 `USER` 来源发布，并保留原候选和审核记录。

默认阈值是 V1 产品规则。修改阈值必须产生配置修订，并只影响后续发布或显式重新评估任务。

### 10.5 社区与 Global GraphRAG（首版后置）

首版不实现或物化社区检测、社区 LLM 摘要、Global GraphRAG 或 DRIFT，也不提供社区浏览。
这些能力只有在固定问题集的消融评测证明它们相对 PageTree/FTS/Wiki/局部图谱显著提高跨文档
主题问答，并满足增量更新、引用完整性、延迟和成本门禁后，才能通过独立 ADR/规格加入。

## 11. 检索与问答

V1 图谱查询能力限定为：

- 按名称、别名和类型查找节点；
- 1–2 跳局部邻域；
- 从节点、边和主张下钻到证据；
- 图谱辅助问答。

目标检索数据流如下：

```text
用户问题
   │
   ▼
Query Planner（固定 generation 快照；可选 LLM 生成 Retrieval Plan）
   ├── Wiki / FTS5 基线召回 ───────────┐
   ├── 语料级 PageIndex PageTree ──────┴──► 基线候选（保证 K_base）
   │       产品 / 版本 / 文档
   └── 正式知识图谱 ──────────────────────► 图谱候选（K_graph 独立预算）
           实体 / 别名 / 已发布边 / 1～2 跳
                           │
基线候选 ──────────────────┼───────────────── 图谱候选
                           ▼
                 CandidateDocument 归一化
                 ├─ document_version_id
                 ├─ channel / rank / reason
                 └─ EvidenceRef 或 locator hint
                    │
                    ▼
       去重 + Reciprocal Rank Fusion
       保证 K_base，不让图谱挤掉基线候选
                    │
             可选有界 LLM 重排
            失败时保留 RRF 顺序
                    │
          ┌─────────┴──────────┐
          ▼                    ▼
 Wiki/FTS 已定位 Evidence   文档级 PageIndex PageTree
 （仅树降级时直达）              │
                          TreeSearchStrategy
                          ├─ 小树：确定性层级检索
                          └─ 大树：ConDB-style Block Retrieval
          │                    │
          └─────────┬──────────┘
                    ▼
             Evidence Pack
          ├─ EvidenceRef / 原文片段
          ├─ 来源、页码、检索通道
          └─ 发布状态与降级状态
                    │
                    ▼
             回答与稳定引用
```

Wiki、FTS、语料级 PageIndex PageTree 和图谱是四条逻辑通道；图中只为展示而把 Wiki/FTS 合并。前三条共同构成基线候选池并受最低配额 `K_base` 保护，图谱使用独立预算 `K_graph`，不得通过共享上限挤掉基线候选。语料级 PageIndex PageTree 负责定位产品、版本和文档，文档级 PageIndex PageTree 负责在候选文档内定位证据。语料树阶段可只提供 locator hint，但进入最终 Evidence Pack 前必须解析为稳定 `EvidenceRef`。

FTS5 同时使用 `unicode61` 与 `trigram` 索引，分别覆盖英文/数字词界和中文、名称、连续字符串
及子串。LLM Retrieval Plan 只生成意图、词语扩展、实体、概念、时间范围和问题类型，不直接
读取数据库或生成查询语言。实体消歧先由名称规范化、别名、类型、FTS 和字符相似度生成少量
候选，再允许一次结构化 LLM 判断；D3 候选只能进入审核，禁止自动合并。

查询开始时在一个短 SQLite 读事务中一次性固定 current generation ID 组合，事务结束后只使用这些不可变 ID，避免一次查询混读新旧 generation。跨类型 generation 可独立推进，但必须通过语料 manifest 和显式依赖关系验证兼容；不兼容的图谱或派生视图按通道降级，不能靠时间戳猜测兼容性。`TreeSearchStrategy` 对小树使用确定性层级检索，对大树使用有块数、字符数和时间预算的 ConDB-style Block Retrieval。文档级 PageTree 不可用时，直接使用 Wiki/FTS 已定位的 Evidence 构造证据包并记录树能力降级。

图谱路径必须有最大跳数、最大扩展节点数和超时，避免高连接节点拖垮桌面应用。所有通道统一输出带稳定文档版本和 `EvidenceRef` 的候选；无法提供证据的图路径不得进入回答上下文。

回答生成器接收的是带来源和状态的证据包，不直接读取未发布图。每条引用解析到稳定 `EvidenceRef`，可打开原文页或片段。若图谱不可用、超时或被关闭，问答自动退回基线通道，并记录降级原因。

## 12. 原生桌面应用

### 12.1 技术选择

- Tauri 2 提供 Desktop Shell、原生窗口、系统托盘、文件对话框、单实例和 Python Engine 监管；
- React/Vite 提供现代工作台，选择性复用旧前端的设计令牌、主题、i18n 和纯呈现组件；
- Python Engine 继续承载 Application Service、阶段调度、模型、存储、检索和知识协调；
- Desktop Bridge 是唯一跨运行时命令、查询、错误和事件边界，生产包不启动或调用旧 Web 服务；
- 首版不提供图谱浏览或渲染，Knowledge Graph 只作为不可见的检索增强通道。

### 12.2 已确认布局

桌面框架采用 A 型工作台：

- 顶部：知识库选择、全局搜索、本地运行健康状态；
- 左侧固定导航：总览、文档、问答、知识页、审核、设置；
- 中央主工作区：当前功能、导入状态、文档、知识页和问答结果；
- 右侧检查器：质量、诊断、来源、证据和选中对象详情；
- 底部全局任务中心：进度、暂停、继续、取消和展开详情。

后台任务不得直接更新 React 组件，只通过 Desktop Bridge 发布版本化快照和有序事件。有任务时
关闭窗口隐藏到系统托盘并继续，空闲时关闭窗口退出；明确退出必须提交当前原子步骤并终止
Python Engine 与全部 worker。

### 12.3 CLI 边界

CLI 与 GUI 调用同一应用服务。桌面正式替换后，CLI 保留：

- 数据库迁移和完整性检查；
- CAS 校验与回收；
- 备份与恢复；
- 失败阶段重跑和批处理；
- 派生视图重建；
- 质量回归和诊断导出。

CLI 不维护第二套发布、审核或写入逻辑。

## 13. 错误处理与可观测性

错误记录必须包含阶段、结构化错误码、类别、可重试性、影响能力、发生时间和脱敏上下文。GUI 使用同一错误模型区分：可搜索、知识就绪、待审核、降级和失败。

关键规则：

- 重试只作用于失败阶段、分区或模型调用，不重跑整份文档；
- PageTree JSON 修复或质量门禁失败时标记降级，不能静默成功；
- 单份问题文档进入隔离区，不阻塞批次中的其他文档；
- 工作者产物先进入临时 CAS，验证后通过短事务提交；
- Publisher 只切换完整 generation；
- 用户可暂停、继续、取消、重新执行失败阶段和重建派生视图；
- 重新执行创建新修订，不能就地改写发布历史；
- 本地日志脱敏并轮转，不记录完整文档正文或密钥；
- SQLite 定期执行完整性检查和在线备份。

右侧检查器显示错误、尝试次数、重试时间、阶段输入版本、输出 generation 和相关证据。任务中心提供每阶段耗时、队列等待、模型调用次数、令牌、重试、降级和失败计数。

## 14. 迁移与分级发布

### 14.1 发布等级

- **R0 基础层**：质量基线、SQLite 候选权威、CAS、Stage DAG、恢复、全局 Model Gateway
  和 Profile 管理。新旧数据流程继续影子对照，不改变当前用户入口；Model Gateway
  可以作为内部统一调用边界全局生效，但这不等于提前切换存储或导入权威。
- **R1 核心 Beta**：桌面端导入、文档管理、搜索、PageIndex 问答和任务中心。达到旧版问答门禁后才可发布。
- **R2 图谱 Beta**：证据化图谱抽取、审核、1–2 跳局部检索和图谱辅助问答；知识库级开关默认保守，无图谱 UI。
- **R3 候选版**：迁移现有 OpenKB 数据，桌面端成为默认入口；旧 WebUI 只读保留用于对照和紧急回退。
- **R4 正式版**：桌面核心质量、功能和 Windows 打包验收通过后，删除旧 CLI、React WebUI
  及专用 REST/SSE 接口；Legacy Knowledge Base 迁移另立后续计划，不作为删除门禁。

任一等级失败只阻止升级，不回滚已验证的权威数据或上一代可用知识。

### 14.2 迁移规则

- 迁移前生成 SQLite/CAS 完整备份、内容散列清单和旧知识库快照。
- 迁移使用可恢复批次日志和幂等键；中断后从最后成功批次继续。
- 保留旧页面标识、路径、别名、wikilink 和 PageIndex 产物。
- 能解析目标的旧 wikilink 以 `LEGACY_IMPORT/RELATED_TO` 导入并保留其页面证据；分数 1.0 只表示“原文明确存在该链接”，不表示更强的语义事实。
- 其他无法从旧数据证明支持度或语义类型的关系保持未评分候选，进入审核而不进入默认正式图；不得伪造 LLM 来源或高置信度。
- 每批核对文档数、内容散列、链接、PageTree、引用和基线问题答案。
- 权威切换前旧库保持可用；权威切换后不做双写，SQLite 成为唯一权威。
- 正式切换后的回退方式是恢复已验证备份或运行兼容应用版本，不能让旧 Markdown 覆盖新数据库。

## 15. 开发与发布环境

- 正式基线：Windows 10 22H2 x64 与受支持的 Windows 11 x64、Tauri 2、固定 WebView2、
  React/Vite、冻结的 Python Engine 和启用 FTS5 的 SQLite。
- 使用 `uv` 管理环境和锁定依赖；所有运行时依赖继续精确固定版本。
- Windows 包必须在原生 Windows 构建和验收，不能把 Linux/WSL 产物视为正式包。
- 首版使用 PyInstaller `onedir` 冻结 Python Engine，并由原生 Windows 构建步骤把 Tauri 入口、
  固定 WebView2、Engine、资源和许可证组装为版本化 portable ZIP；用户环境不要求 Python、
  Node.js、Rust、安装器或独立服务。
- 参考机器为 8 核 CPU、16 GB 内存和 SSD；GPU 可选。
- 云端 API、本地兼容 API 和本地模型统一经 Model Gateway；无网络时仍可管理、搜索、查看和导出已有知识。只有配置了可用本地模型时才承诺离线生成式问答。需要模型的新抽取任务应明确进入等待或降级，而非伪造完成。

## 16. 测试与验收

### 16.1 测试层次

- 单元测试：状态机、幂等键、CAS、谓词约束、支持度聚合、发布规则和引用构造。
- 契约测试：转换器、PageIndex、Model Gateway、Agents SDK bridge、Profile Registry
  和旧版迁移适配器的边界形状。
- 集成测试：多格式解析、导入、增量更新、重复文件、审核、局部图谱问答、Markdown 物化、备份恢复。
- 故障注入：进程终止、Windows 重启、数据库繁忙、磁盘不足、模型超时/限流/5xx/无效 JSON。
- 迁移测试：可重复执行、中断恢复、旧 ID/链接保留、回退备份完整性。
- 桌面测试：视图模型、事件队列、后台任务、关闭恢复和 Windows 打包冒烟测试。

### 16.2 验收门槛

- 中等规模：约 1,000 份文档、100,000 页、50,000 个正式节点、250,000 条正式边。
- GUI 主线程不得执行转换、模型或图计算；任务状态从后台到界面的更新延迟不超过 1 秒。
- 50,000 个正式节点、250,000 条正式边时，受预算的 1–2 跳局部图谱检索不能冻结任务中心。
- 崩溃后不得出现半发布 generation；重复执行不得产生重复业务对象。
- SQLite 与 `raw/` 联合备份可恢复；FTS 和 Markdown 可从权威数据全部重建。
- 默认模型调用严格遵守首次加 3 次重试和 60 秒硬截止；错误分类不得产生隐藏 repair/retry。
- 所有 OpenKB 模型调用通过 Gateway；除 LiteLLM transport 外无直接 LiteLLM/
  `LitellmModel` 生产旁路。
- Profile 迁移前后所有现有 Prompt、默认 AGENTS、内置 SKILL/references 和组装后的
  Agent instructions 逐字节一致。
- 正式知识的证据可打开率为 100%。
- 问答质量满足第 5 节全部不倒退门禁。
- 除用户配置的模型服务和 URL 导入外，不产生隐式外联；默认不上传知识库、日志或遥测。

### 16.3 发布阻断条件

出现以下任一情况必须阻断升级：

- 权威数据约束或完整性检查失败；
- 迁移清单、内容散列或关键链接不一致；
- 基线问答质量或关键证据召回下降；
- 存在未标记的静默降级；
- 崩溃恢复产生重复发布或半发布数据；
- Windows 正式包不能在干净系统上启动和完成核心流程。

## 17. 安全与本地优先

- 密钥进入系统凭据或受限本地配置，不写入 SQLite 正文、日志或导出 Markdown。
- 所有外联适配器均由用户显式配置，并在 GUI 显示目标、模型和当前网络状态。
- URL 导入限制协议、响应大小、重定向次数和超时；下载内容仍先进入隔离解析流程。
- 打开 CAS 或导出路径前执行路径规范化，防止越界访问。
- 模型输入记录散列和元数据，不默认保留完整请求正文；诊断包导出前允许用户预览和脱敏。
- 本地日志、备份和导出由用户控制保留周期。

## 18. 实施分解

本设计是跨发布等级的主规格，范围过大，不应形成一个巨型实施计划。实施按依赖顺序拆分，每个等级独立规划、验收和提交：

1. R0-A：旧版质量基线、固定语料、问答回归框架；
2. R0-B：SQLite/CAS 权威模型、迁移框架和备份；
3. R0-C：Stage DAG、单写者、幂等、恢复和 Model Gateway；
4. R1：Application Service 与桌面核心工作台；
5. R2-A：证据化图谱抽取、身份协调和分级发布；
6. R2-B：vectorless 局部图谱检索、RRF/LLM 重排、图谱辅助问答和质量消融；
7. R3：旧知识库迁移、影子对照和默认入口切换；
8. R4：删除旧界面层和专用 Web API，完成正式 Windows 发布。

### 18.1 R0 当前状态与权威输入

| 子阶段 | 状态 | 权威规格/记录 |
| --- | --- | --- |
| R0-A 质量基线 | 自动化开发完成 | `docs/superpowers/plans/2026-08-08-r0-a-quality-baseline-regression.md` |
| R0-B 候选权威存储 | 自动化开发完成 | `docs/superpowers/specs/2026-08-09-r0-b-authoritative-storage-design.md` 与 `docs/superpowers/plans/2026-08-09-r0-b-authoritative-storage.md` |
| R0-C1 Stage Runtime Kernel | 自动化开发完成 | `docs/superpowers/specs/2026-08-11-r0-c1-stage-runtime-kernel-design.md`、`docs/superpowers/plans/2026-08-11-r0-c1-stage-runtime-kernel.md` 与 `docs/testing/r0-c1-acceptance.md` |
| R0-C1 transactional outbox 修正 | 自动化开发完成 | `docs/superpowers/specs/2026-08-12-runtime-event-outbox-design.md` 与 `docs/superpowers/plans/2026-08-12-runtime-event-outbox.md` |
| R0-C2 Model Gateway + Profile 管理 | 设计已确认，单一实施计划已生成，待实施 | `docs/superpowers/specs/2026-08-13-r0-c2-model-gateway-profile-management-design.md` 与 `docs/superpowers/plans/2026-08-13-r0-c2-model-gateway-profile-management.md` |

R0-C2 之后的真实 S0–S7 Executor 必须另行设计；其编号和范围尚未确认。R0-C2 不得
预实现真实 Stage 或生产权威切换。R3 仍负责旧知识库迁移、影子对照和默认入口切换。

### 18.2 R0-C2 单一计划与强制里程碑

R0-C2 形成一个实施计划，但 C2a 验收未通过时不得开始 C2b。

**R0-C2a：全局 Model Gateway 与全调用迁移**

1. Model/Profile/Call/Error 类型和确定性 retry/deadline primitives；
2. schema v4、ModelCall/Attempt/session repository、integrity 与 exact backup/restore；
3. store 级 AuthorityWriter 仲裁并保持 R0-C1 RuntimeWriter/fencing 行为；
4. LiteLLM transport 与普通、异步、流式 ModelGateway；
5. OpenAI Agents SDK GatewayModel bridge；
6. Compiler 全调用迁移并消除模型错误的整份编译双重重试；
7. Query、Chat、Linter 全调用迁移；
8. Skill、Deck、Evaluation 全调用迁移；
9. 直接调用旁路扫描、并发 KB 隔离、故障注入和 C2a acceptance。

**R0-C2b：现有模型资源原样迁出与统一管理**

10. 冻结全部 materialized Prompt、默认 AGENTS、内置 SKILL/references 和 assembled
    Agent messages 的长度、字节与 SHA-256；
11. 严格 Profile Registry、版本身份、依赖和 package resource layout；
12. Python 内嵌 Prompt 与现有 `openkb/prompts/` 资源原样迁出；
13. `openkb.schema.AGENTS_MD` 原样迁出并保持 init/refresh/custom 行为；
14. 内置 SKILL/references 原样迁出并保持搜索优先级与 override 行为；
15. Profile list/show/status/verify/pin/upgrade/rollback 应用服务和 shadow CLI；
16. wheel/fresh-install、custom preservation、逐字节等价和 R0-A/R0-B/R0-C1/全量门禁。

C2b 的任务 10–16 只建设管理机制，禁止对当前默认内容做任何优化。未来内容优化必须
创建新的 Profile 版本、使用独立规格，并通过 R0-A 对照门禁。

后续等级必须在前一等级自动化门禁通过后分别制定实施计划。Windows 实机和生产语料
人工验收可按维护者决定作为非阻塞跟踪项，但不能被宣称为已通过的正式发布门禁。

## 19. 最终不变量

1. SQLite 是唯一业务权威。
2. 原始资产和大型产物按内容寻址保存。
3. Markdown 只是可重建视图，不反向覆盖权威数据。
4. 所有知识结论都能追溯到证据、来源和修订。
5. LLM 不持有业务事务，失败可有限重试且不能静默成功。
6. vectorless 图谱增强 PageIndex/FTS/Wiki，不在质量证明前替代基线；不存在 Embedding 或向量索引旁路。
7. React、Tauri 与 Python worker 通过 Desktop Bridge 和同一应用服务共享领域规则，Rust 不复制业务逻辑。
8. 后台任务不阻塞 Desktop Shell 或 React 渲染线程。
9. 发布只切换完整 generation。
10. 每个发布等级都受数据、迁移、质量和 Windows 验收门禁约束。
11. OpenKB 发起的所有模型请求都经过全局逻辑 Model Gateway，且不共享可变 KB
    credentials/config。
12. Gateway retry、Agent turn 和 Stage retry 使用独立身份与预算，且总等待始终受有限
    deadline 约束；响应格式错误不得触发隐藏 repair round。
13. Profile 身份解析为具体版本与 SHA-256；R0-C2b 迁移现有 Prompt、AGENTS 和 Skill
    时内容逐字节不变，用户自定义资源不被自动覆盖。
