# OpenKB 桌面核心功能与体验升级设计

- 状态：已确认，待制定实施计划
- 日期：2026-08-14
- 目标平台：Windows 10 22H2 x64 与仍受支持的 Windows 11 x64
- 产品形态：单机、单用户、本地优先
- 上游设计：`2026-08-08-openkb-local-knowledge-graph-desktop-design.md`

本文是桌面核心产品范围的增量权威规格。与上游设计冲突时，本文及
`docs/adr/0001`–`0022` 的已确认决策优先；未被修改的架构原则继续沿用。

## 1. 结果摘要

首个 Desktop Workbench 提供一个完整的本地工作闭环：

1. 创建并选择新格式知识库；
2. 从桌面导入单个文件、多个文件或目录；
3. 后台分阶段处理，自动处理临时模型故障；
4. 失败文档进入可跨重启恢复的隔离列表；
5. 已成功文档无需等待同批其他文档，立即进入问答；
6. 问答使用普通文档检索和不可见的图谱增强检索；
7. 答案展示原文图片、文档名和章节引用；
8. 用户可以编辑概念与实体知识页，并审核知识冲突；
9. 有后台任务时关闭窗口默认进入系统托盘。

桌面首版不提供图谱浏览、Skill Factory、Deck 生成、文档删除或旧知识库迁移。

## 2. 产品范围

### 2.1 首版功能

- 知识库创建、选择和基础健康状态；
- 文档导入、去重、阶段进度、暂停/继续和失败恢复；
- 文档列表、原文阅读与独立保存的原文图片；
- 多轮问答、流式回答、回答重试和会话恢复；
- 概念与实体知识页查看和编辑；
- 知识冲突的逐项与批量审核；
- 模型、凭据、并发和超时设置；
- 全局任务中心、失败文档菜单和系统托盘运行。

### 2.2 明确不做

- 不显示知识图谱；
- 不提供 Skill Factory 或 Deck 生成；
- 不删除已完成导入的文档；
- 不迁移或打开 Legacy Knowledge Base；
- 不依赖浏览器、本地 Web 服务或独立常驻后端；
- 不支持多用户、远程协作或权限体系。

## 3. 桌面信息架构

桌面采用内容优先的单窗口工作台：

- 顶部：Active Knowledge Base 切换、`Ctrl+K` 全局搜索、任务状态与 Engine 健康；
- 左侧：总览、文档、对话、知识、设置，可折叠为图标导航；
- 中央：当前功能的主要内容，不重复知识库名称、路径、Schema、检查点或运行说明；
- 对话页：可折叠 Conversation 列表、时间顺序消息流和固定在底部的输入框；
- 右侧按需抽屉：Answer Evidence 或文档详情，不设置永久检查器；
- 全局任务按钮：打开底部任务抽屉，展示 Import Batch、Stage Run、进度和控制操作；
- 文档页失败入口：展示隔离文档、失败原因、尝试记录和手动恢复。

总览只展示可行动信息，包括可用、处理中、失败和待审核数量、最近活动，以及导入文档和
开始对话的快捷操作。知识库路径、Schema、存储和诊断信息只在设置或诊断界面出现。
文档页使用紧凑导入工具栏和可搜索、按状态筛选的文档列表；点击文档打开右侧详情抽屉。
“知识”内部以“知识页”和“待审核”为一级标签，后者再区分知识冲突和文档版本候选。
设置按“模型连接”“外观与语言”“存储与诊断”分组，模型连接支持使用尚未保存的当前输入
执行测试连接。短暂成功、恢复和切换结果使用 Toast，就近可解决的错误在当前操作附近展示，
后台状态使用顶栏徽标；只有无法进入工作台的启动失败占用整页。

全局搜索覆盖当前知识库的文档、知识页、Conversation 标题和用户问题，并按类型分组；设置、
运行日志和内部任务不进入普通搜索结果。视觉层级以内容为主，减少嵌套卡片和装饰性大标题，
动画约 150ms 并尊重系统“减少动态效果”。

失败恢复与知识审核必须是两个不同入口：前者处理运行故障，后者处理内容冲突。

首版只有一个主窗口，不提供可拆卸面板或多窗口同步。文档、问答、知识页、审核和原文
位置都在同一工作台内切换或通过模态对话框完成。用户可以把文件或目录拖入工作台创建
Import Job；首版不注册文件关联、Shell 右键菜单或开机启动项。

工作台保留简体中文和英文，默认跟随系统语言并允许手动切换；所有桌面状态、错误分类和
建议操作必须同时提供两种文案。主题支持浅色、深色和跟随系统，界面缩放支持 80%–200%
及 `Ctrl +`、`Ctrl -`、`Ctrl 0`。首版无障碍门禁包括完整 Tab 顺序、可见焦点、ARIA
语义、常用快捷键，以及状态表达不能只依赖颜色，但不声明完整 WCAG 认证。

Markdown 渲染默认禁用原始 HTML、脚本、远程图片和自动网络请求，只允许清洗后的 Markdown
与 Desktop Shell 授权的 Source Image。普通外部链接显示目标域名后交给系统浏览器打开，
不得在 Desktop Workbench 内导航到远程页面。

应用可恢复窗口位置与大小、主题、语言、缩放、导航折叠状态、最后一个功能页、最后一个
Conversation 和未发送的问答草稿；草稿不得自动发送。已有知识库恢复最后页面，新建知识库
直接进入文档导入。未保存的 API 密钥和未提交的 Conflict 批量选择不作为普通 UI 状态写入
应用设置，后者只能通过 Review Queue 自身的权威暂存机制持久化。

## 4. 导入与存储

### 4.1 原始文件

每份成功导入的完整原文只保留一份，位于 `raw/`。SQLite 保存其散列、长度、
身份和生命周期元数据；CAS 只保存大型派生产物，不保存第二份完整原文。

- 首版不提供已完成文档删除；
- SQLite 与 `raw/` 是一个不可分割的备份/恢复单元；
- `raw/` 文件缺失或散列不匹配时，文档进入隔离状态；
- 系统不能从 SQLite 或 CAS 重建丢失的完整原文。

### 4.2 四级去重

- D0：原始资产散列完全相同，复用已有资产；
- D1：规范化正文散列完全相同，复用已有文档处理结果；
- D2：页面或证据片段重复，复用证据且不能重复增加支持度；
- D3：语义近似候选，只进入提示或审核，禁止自动合并。

D0–D3 只判断内容重复，不判断文档身份。D3 可产生 Document Version Candidate；
用户确认后才归入已有文档版本链，否则作为新文档导入。实体和概念相似度不能代替
文档版本归属决定。

### 4.3 阶段级执行

导入采用可持久化的 Import Job 和 Stage Run。已验证完成的阶段形成检查点，恢复时不得
重跑。至少区分：资产登记、转换、证据、基础检索、PageIndex、知识抽取、图谱抽取、
协调发布和派生视图。

必需阶段失败会隔离文档，隔离文档不得参与问答。图谱等可选增强阶段失败只形成内部
能力降级，文档仍可通过普通检索参与问答。

### 4.4 模型调用预算

每个逻辑 Model Call：

- 首次调用不计入重试次数；
- 最多执行首次调用加 3 次自动重试；
- 首次响应超时默认 20 秒；
- 每次重试将请求超时增加 10 秒；
- 整个逻辑 Model Call 有 60 秒硬截止时间，截止时间优先于剩余重试次数；
- 只自动重试超时、限流、临时网络错误和可重试服务端错误；
- 鉴权、配置和响应格式错误直接失败并隔离，不自动重试。

每个 Model Attempt 都记录状态、耗时、脱敏错误和关联 Stage Run。请求正文、密钥和
完整响应不得进入日志或 SQLite。

### 4.5 批次可用性

Import Batch 只是进度队列，不是发布边界。同批 20 份文档中，即使仍有 3 份运行、
2 份隔离，已成功的 15 份也立即成为 Available Knowledge。问答界面不显示“知识库尚未
完整”的全局提示，因为知识库被视为持续演进的集合。

### 4.6 文档解析

解析与 Evidence 分块是两个阶段。每个格式专用 `Parser Adapter` 先把 Raw Asset 转换为统一
`Document IR`；IR 按原始顺序保存 heading、paragraph、list、code、table、figure，以及可用的
page、slide、sheet、cell range、bbox 和 Source Image 引用。Markdown 只是从 IR 物化的阅读
视图，不能作为解析权威。

首版格式路由如下：

- `.txt`：编码检测、换行规范化和段落提取；
- `.md` / `.markdown`：保留标题、列表、代码围栏、Markdown/HTML 表格和相对图片；
- `.docx`：使用 `python-docx` 按正文顺序提取标题、段落、表格和嵌入图片；
- `.xlsx`：使用 `openpyxl` 保留工作表、表头、合并单元格、单元格坐标、缓存值、公式文本和图片；
- `.xls`：使用 `python-calamine` 读取工作表和单元格值；
- `.pptx`：使用 `python-pptx` 按 slide 和 shape 几何顺序提取文本、项目符号、表格、图片和备注；
- `.pdf`：先运行 PyMuPDF 快速文本路径；扫描件、乱码、低文本密度或用户手动选择增强解析时，
  使用随包交付的 DeepDoc ONNX OCR、版面识别和表格结构识别能力；
- `.doc` / `.ppt`：使用随包交付的 `python-tika`、私有 Tika Server JAR 和精简 Java Runtime
  做低保真文本与元数据兼容读取，不承诺图片、表格、页码、幻灯片或版面。

不打包或调用 LibreOffice，不要求用户安装 Java、Python 或解析服务。Tika 仅由 Python Engine
按需启动和监管，不开放公共端口；所有 JAR、Java Runtime、ONNX 模型及本地 wheel 都必须随
Portable Desktop Package 固定版本交付，禁止首次使用时联网下载。

普通 PDF 从快速路径切换到增强路径属于解析路由，不计作错误重试。损坏文档、不支持格式、
Tika 返回空内容或 Parser Adapter 无法产出有效 Document IR 时，按格式错误直接隔离且不自动
重试。失败详情提供“转换为 DOCX/PPTX 后重试”等可行动建议。

## 5. 失败恢复体验

自动恢复耗尽后，文档成为 Quarantined Document：

- 不参与搜索上下文、图谱发布或 Grounded Answer；
- 失败文档菜单展示阶段、原因、尝试次数和建议操作；
- 应用重启后仍可查看；
- 用户可修改模型或超时并手动恢复；
- Recovery Override 只作用于该次恢复，不修改知识库默认设置；
- 恢复从失败 Stage Run 继续并复用转换、索引等已验证输出。

## 6. 问答体验

### 6.1 检索

OpenKB 保持 vectorless：不配置或调用 Embedding 模型，不建立向量索引，也不引入向量数据库。
这里的“语义召回”专指受限 LLM 查询规划、PageTree 推理、概念/实体扩展和候选重排。

每次问答先尝试让 LLM 生成结构化 Retrieval Plan，包含意图、词语扩展、实体、概念、时间范围
和问题类型。随后并行运行 FTS5、PageTree、Wiki/知识页和正式 Knowledge Graph 四条逻辑通道：

- FTS5 同时维护 `unicode61` 与 `trigram` 索引，兼顾英文/数字词界和中文、名称及子串；
- PageTree 负责语料级文档定位和文档级证据定位；
- Wiki/知识页使用规范名称、别名、概念和已发布内容召回；
- 图谱只从已经命中的 Evidence、实体或别名锚点进行有预算的 1–2 跳扩展。

各通道输出统一候选，经规范化、D2 去重和 Reciprocal Rank Fusion 后保留基线最低配额，
再允许一次有界 LLM 重排形成 Evidence Pack。Retrieval Plan 或重排失败时直接使用确定性
查询词和 RRF 结果，不能阻断回答。普通文档检索始终是安全基线。

Knowledge Graph 直接从 Evidence Fragment 抽取 Entity、Concept、Claim、TypedEdge 和
EvidenceRef 候选，不从物化知识页反向推导权威事实。图谱作为不可见的附加检索通道参与问答，
桌面端不提供图谱浏览。首版只实现证据锚定的局部 1–2 跳 Graph-Augmented Retrieval；社区
检测、社区 LLM 摘要、全库 Global GraphRAG 和 DRIFT 检索延后到固定评测证明增益之后。

- 图谱候选必须解析到原始证据；
- 图谱抽取、查询失败或超时时，自动退回普通文档检索；
- 图谱内部失败不向用户展示，但必须保留脱敏诊断记录；
- 图谱不能挤掉基线检索的最低候选配额。

实体消歧不使用向量：先用规范化名称、别名、类型、FTS 和字符相似度产生小规模候选，再允许
LLM 对候选给出结构化判断。D3 Entity Resolution Candidate 永远进入 Review Queue，不自动合并。

### 6.2 答案完成态

完成的 Grounded Answer 按以下顺序展示：

1. 使用 GFM Markdown 呈现的答案正文，其中有效 `[n]` 可打开对应 EvidenceRef；
2. 最多 3 张来自已引用原文章节的 Source Image 缩略图；
3. 紧凑的“引用 N · 图片 M”按钮，打开该回答的 Evidence Drawer。

Evidence Drawer 只展示真正发送给回答模型的 Answer Evidence，并分为“来源”和“原图”。
来源显示文档、章节、位置与短摘录，点击后打开原文；检索通道、分数和内部降级只进入诊断。
无效引用编号不生成链接并记录诊断，但不因此丢弃整个回答。宽窗口使用 420px 覆盖抽屉，
窄于约 1100px 时改为全高覆盖层，不压缩回答正文。

Source Image 必须在导入时独立保存。禁止生成替代图片或展示无法绑定到引用章节的图片。

### 6.3 流式中断与重试

模型超时、请求失败或用户停止时，已流出的文本保留为 Interrupted Answer，并显示明确的
中止状态。用户点击重试后重新发起完整请求；中止答案继续保留，直到重试成功的完整答案
在原位置替换它。重试失败时仍保持中止状态，不伪装成完成答案。

标题、列表、表格和代码等 Markdown 在流式生成时持续渲染；未闭合的 KaTeX 与 Mermaid
显示占位，回答完成后再正式渲染。流式、完成和重试使用同一个 Markdown 呈现边界。

### 6.4 Conversation 与 Answer Version

每个 Desktop Knowledge Base 可以持久化多个 Conversation。Conversation 以时间顺序保存用户
消息和助手消息，支持搜索、新建、重命名和删除，并按今天、最近 7 天和更早分组；首版不提供
置顶、归档、文件夹、编辑历史问题或会话分支。现有扁平 Grounded Answer 不迁移到新的
Conversation 模型。

每轮可使用最近 4 轮已完成问答作为 Conversation Context 来理解指代，但必须针对当前问题
重新检索 Available Knowledge；Interrupted Answer 不进入上下文。切换 Conversation 不会中止
正在生成的回答，每个 Conversation 同时最多运行一个回答任务。

Grounded Answer 与其 Answer Evidence 是不可变快照。重新生成成功会为同一助手消息创建并选中
新的 Answer Version，旧版本继续可选；重新生成失败时保留当前版本。后续 Conversation Context
使用用户当前选中的版本。文档或知识更新不改写旧回答，新问题和重新生成使用最新 Available
Knowledge；来源当前不可用时只显示状态提示。

## 7. 知识页与冲突审核

### 7.1 用户编辑

桌面首版保留概念和实体 Knowledge Page 编辑，不提供原始文档或摘要直接编辑。编辑操作
创建 SQLite 中的 User Revision，再从权威数据重新物化 Markdown；不得直接把 Markdown
修改作为权威输入。

### 7.2 知识协调

新文档或新 Document Version 带来的实体、概念更新执行 Knowledge Reconciliation：

- 非冲突新增内容可以自动合并；
- 与已发布知识或 User Revision 不兼容的内容成为 Conflict；
- Conflict 进入 Review Queue，支持逐项选择和批量选择；
- 批量选择在提交前只是暂存，不修改已发布知识。

用户提交审核后，未采用的派生概念/实体候选内容被物理删除。系统仍保留原始文档、
Document Version、EvidenceRef 和最小 Resolution Record。被删除候选若需恢复，必须重新
执行抽取。

## 8. 应用生命周期

Desktop Runtime 是一个由用户统一管理的应用生命周期，而不是“只能有一个 OS 进程”。
唯一 `.exe` 入口可以启动随包交付的 UI、后台调度和工作者子进程，但不得开放本地 HTTP
端口、注册系统服务或依赖用户单独管理的后端。主程序退出时必须终止全部子进程；崩溃恢复
不得遗留孤儿进程。

桌面框架固定为 Tauri 2 + React/Vite + Python Engine：

- Desktop Shell 使用 Tauri 2，负责窗口、托盘、文件选择、通知、单实例、Python Engine
  生命周期、安全 IPC 和发布包入口；
- React/Vite 负责全部工作台视图、路由、状态呈现和交互，生产包只加载随包静态资源，
  不启动 Vite 或其他 Web 服务；
- Python Engine 作为随包交付的受管子进程，承载应用服务、SQLite、导入调度、LLM、检索、
  知识协调和 Markdown 物化；
- Rust 层不得复制或承载 OpenKB 领域规则，只实现原生壳层、进程监管和类型化桥接；
- 原有 React/Vite 代码只复用仍符合 Desktop Workbench 信息架构和状态模型的部分，不保留
  旧 REST/SSE 客户端边界。

Tauri 依赖使用实施时验证通过的稳定版本并精确固定，提交 Rust、Node 与 Python 的完整锁文件。
`tauri-plugin-single-instance` 必须最先注册；第二次启动只通过 Rust 回调转交启动意图和聚焦
主窗口。

前端迁移保留 React/Vite 工程、Tailwind 设计令牌、Radix/shadcn primitives、主题、i18n、
图标和不依赖旧 API 的纯呈现组件；删除旧业务页面、`src/api` 传输层、连接对话框、REST/SSE
状态和 Skill、Deck、图谱可视化交互。迁移时删除未使用的 UI primitives 与依赖，保留依赖
全部精确固定并提交 lockfile。

Markdown 使用成熟的 AST 管线重新实现，支持 GFM，并通过严格清洗白名单扩展 KaTeX、Mermaid、
WikiLink、EvidenceRef 和 Source Image。旧自制渲染器只作为行为参考，不继续扩展；原始 HTML、
脚本、远程图片和未授权本地资源在解析与渲染两层都必须被拒绝。

React 只能通过单一 Desktop Bridge 提交命令、读取查询并订阅事件。Python 的版本化请求、响应、
错误和事件模型是协议 schema 权威，并生成 TypeScript 与 Rust 类型；组件不得直接拼接 JSON、
调用 sidecar 或读取任意本地路径。Desktop Bridge 在 React 测试中必须可替换为内存实现。

React 到 Rust 的领域命令使用窄化 Tauri Commands；导入进度、流式回答和其他有序高吞吐数据
使用 Tauri Channels，不依赖无顺序保证的通用 Events。任何 `VITE_*` 环境变量都会进入前端
产物，因此不得承载 API 密钥、凭据引用或其他秘密。

前端使用集中式 Workbench Store：先读取带 revision 的权威快照，再按单调 sequence 应用事件；
检测到事件丢序、协议重连或 Python Engine 重启时丢弃增量视图并重新读取快照。窗口、分栏、
草稿等临时 UI 状态与 Python 权威业务状态分别存储，不互相推断。

Desktop Shell 与 Python Engine 之间使用私有、长度前缀的 JSON-RPC over stdio：

- 协议支持请求/响应、取消、流式事件、错误分类和启动版本握手；
- `stdout` 只承载协议帧，脱敏运行日志写入 `stderr`；
- 不开放 TCP/HTTP 端口，也不允许前端绕过 Desktop Shell 直接访问 Python Engine；
- 原文图片和大型派生产物通过 Desktop Shell 授权的本地资源协议读取，二进制内容不进入
  JSON 消息；
- 协议版本不兼容时拒绝进入工作台，并展示发布包损坏或版本不匹配诊断。

Python Engine 在 Desktop Runtime 生命周期内保持运行。Engine 意外退出时，Desktop Shell
先把执行中的 Stage Run 视为可恢复，再自动重启一次并恢复工作台；短时间内连续崩溃则停止
自动重启，展示脱敏诊断和“重新启动引擎”操作，禁止无限崩溃循环。

长期 Python Engine 由专用 Rust EngineSupervisor 直接持有，不能把生命周期委托给 Tauri
Shell 插件的普通子进程清理。Windows 启动时创建启用 `KILL_ON_JOB_CLOSE` 的 Job Object，
Desktop Shell、Python Engine 及其后继 worker 全部处于同一 Job 且禁止 breakaway；正常退出
依次发送 `shutdown`、限时等待、强制终止兜底。强杀主程序后不得残留任何 Engine 或 worker。

Desktop Runtime 使用全局单实例：再次启动入口只唤醒现有主窗口，并把待打开的知识库或
待导入路径转交给已有实例。每个 Desktop Runtime 同一时刻只绑定一个 Active Knowledge
Base；首版不在一个 Python Engine 内并发写入多个知识库。切换前必须把当前前台操作和任务
状态提交到检查点；运行中的 Import Job 在当前原子步骤结束后安全暂停，后台任务权威状态
仍保存在原知识库中，并在用户重新打开该知识库后恢复。切换不启动第二个 Python Engine。

Desktop Shell 立即显示主窗口骨架和“正在启动本地引擎”健康状态，不使用阻塞式启动画面。
Engine 未就绪前禁用导入、问答和知识写入，但允许打开设置与诊断；超过启动期限后展示脱敏
诊断、重试和退出入口。

- 点击主窗口关闭按钮始终隐藏到系统托盘，不根据任务状态改变语义；
- 左键托盘图标恢复并聚焦主窗口，右键只显示托盘菜单；
- 托盘提供打开工作台、查看任务和明确退出；
- 工作台隐藏时，导入完成或产生 Quarantined Document 发送 Windows 汇总通知，只包含知识库
  名称和成功/失败数量；点击后打开任务中心或失败文档菜单，不显示正文、问答或错误原文；
- 明确退出时安全停止派发，提交当前原子步骤，并保存可恢复状态；
- 异常退出或系统重启后，过期运行状态转为可恢复状态；
- 下次启动从已提交检查点继续，不重复已完成阶段。

首版以 Portable Desktop Package 交付：用户下载并解压一个自包含的 Windows 发布包，
通过唯一的 `.exe` 入口启动。目标机器无需预装 Python、Node.js、开发工具或独立服务；
运行所需的框架资源和语言运行时必须随发布包提供。

发布包同时包含固定的 DeepDoc ONNX 解析模型、所需本地推理库、精简 Java Runtime 和 Tika
Server JAR。它们只是 Python Engine 管理的内部文档处理资源，不是用户安装项或独立后端。
发布清单必须逐项校验其版本和散列；离线验收覆盖首次导入，确保没有隐式模型或 JAR 下载。

Tauri 没有原生 portable ZIP 发布目标，因此 Windows 构建流水线必须自定义组装确定性版本目录：
根目录只暴露 `OpenKB.exe` 用户入口，并包含固定 WebView2、冻结的 Python Engine、资源、许可证
和版本清单，完成完整性验证后再生成 ZIP。正式包只能在原生 Windows 环境组装和验收。

完整 UI 实施前必须先完成最小 Windows 打包纵切，验证固定 WebView2、PyInstaller `onedir`
Python Engine、stdio 拆帧/粘帧、流式事件、取消、scoped Source Image、中文/空格路径、
Job Object 清理和最终 ZIP 离线启动。全部通过后才把 Tauri 视为最终落地；关键门禁无法满足时
重新打开框架选择，不在未验证的打包链上继续堆叠功能。

桌面框架选择优先保障现代、丰富、易迭代的工作台体验，不要求主界面严格使用 Windows
原生控件。富 Markdown、原文图片、引用跳转、流式问答、可调整分栏、长列表和任务中心
应优先使用成熟 UI 生态；文件选择器、系统托盘、通知和窗口生命周期仍应采用原生系统能力。

Portable Desktop Package 首版优先自包含和可重复启动，不以最小体积作为发布门禁。允许随包
提供固定 WebView2、Python、DeepDoc ONNX、Java/Tika 和其他文档处理依赖；发布流水线记录
各组件及最终 ZIP 的实际体积，后续版本再以不牺牲解析可靠性为前提裁剪。

首版采用手动更新：用户下载新的版本化 ZIP，在应用完全退出后替换程序目录。Desktop
Knowledge Base 必须位于程序目录之外，升级发布包不得修改知识库。首个可用版本允许未签名；
Authenticode、应用内更新检查和固定 WebView2 安全更新流程留到公开分发硬化，不阻塞当前桌面
功能与打包纵切。

首版使用 Tauri 内置 asset protocol 展示 Source Image，只把当前 Active Knowledge Base 的
派生图片目录加入 scope；不得放行整个知识库、`raw/`、SQLite、应用配置目录或用户目录。
权限模型只保留一个主窗口 Capability，开放必需的自定义 Commands、文件对话框和 scoped
图片，不开放通用 shell 与任意文件系统权限；CSP 使用满足静态工作台运行的最小配置。

程序目录视为只读。应用设置、脱敏日志、崩溃计数、最近知识库列表和非知识库 UI 状态保存在
`%LOCALAPPDATA%\\OpenKB`；知识库路径由用户选择。长期 API 凭据保存在 Windows
Credential Manager/DPAPI，SQLite 和配置文件只保存凭据引用；同时允许只驻留本次运行内存的
临时凭据。旧知识库 `.env` 不作为 Desktop Knowledge Base 的正式凭据存储。

首版不自动上传遥测、日志或崩溃报告。用户可显式生成 Diagnostic Bundle，导出前必须展示
将包含的数据；诊断包排除 Raw Asset、文档正文、模型请求、模型响应、授权头和凭据。

## 9. 旧入口与兼容性

首版只创建和打开 Desktop Knowledge Base，不迁移 Legacy Knowledge Base。桌面核心功能
通过验收后，删除旧 CLI、Web Workbench 及其专用 REST/SSE 接口。旧知识库迁移另立后续
规格和计划；在迁移实现前，旧数据没有受支持的应用入口。

删除门禁至少包括：

- 新知识库可创建、关闭并重新打开；
- 文档导入、隔离、重启恢复和手动重试通过；
- 成功文档可在其他导入运行时参与问答；
- 问答引用、原文图片和中断重试通过；
- Knowledge Page 用户修订和 Markdown 物化通过；
- Conflict 逐项/批量审核通过；
- Windows 原生打包和系统托盘冒烟测试通过。

## 10. 相对上游设计的变更

| 主题 | 上游设计 | 本规格 |
| --- | --- | --- |
| 完整原文 | CAS 保存原始资产 | 只在 `raw/` 保存一份完整原文 |
| 模型尝试 | 默认最多 3 次尝试 | 首次调用加最多 3 次重试 |
| Model Call 截止时间 | 默认 180 秒 | 每个逻辑调用硬上限 60 秒 |
| 批次发布 | 批次协调后发布 | 成功文档独立、立即可问答 |
| 图谱界面 | R2 提供局部图浏览 | 首版无图谱界面，只内部增强检索 |
| 语义召回 | 未固定向量边界 | 保持 vectorless；LLM 规划/重排 + FTS/PageTree/Wiki/图谱 |
| 文档中间层 | 页面/标题/块 IR | 统一 Document IR；按格式 Parser Adapter 生成 |
| 复杂 PDF | 未固定解析器 | PyMuPDF 快速路径 + 随包 DeepDoc ONNX 增强路径 |
| 旧 DOC/PPT | 未固定兼容路径 | python-tika + 随包 JRE/JAR，低保真文本；不含 LibreOffice |
| 文档删除 | 维护流程可移除文档 | 首版不删除完成文档 |
| 知识页编辑 | Markdown 为派生视图 | 编辑写入 SQLite User Revision 后物化 |
| 旧库迁移 | R3 迁移并影子对照 | 首版不迁移，后续另行规划 |
| 旧入口 | 迁移后逐级删除 | 桌面核心验收后删除，不等待迁移 |
| 生成器 | 现有 Skill/Deck 保留 | 首版不提供 Skill/Deck |
| 桌面框架 | CustomTkinter + Tk + PyInstaller onedir | Tauri 2 + React/Vite + 受管 Python Engine |

## 11. 核心验收场景

1. 导入 20 份文档，15 份成功、3 份运行、2 份隔离；15 份立即可用于问答。
2. 必需 LLM 调用超时：按 20 秒起始、每次增加 10 秒重试，60 秒硬截止后隔离。
3. 鉴权或配置错误：不自动重试，失败菜单给出原因和设置入口。
4. 重启后打开失败文档菜单，使用本次 Recovery Override 从失败阶段继续。
5. 图谱抽取失败：文档仍可通过普通检索回答，界面不显示图谱错误。
6. 回答引用原文图片、文档和章节；无法绑定证据的图片不得展示。
7. 流式回答中断后保留部分内容；成功重试在原位置替换中止答案。
8. 导入改名后的近似文档，D3 只产生版本候选，经用户确认才进入原版本链。
9. 实体或概念出现冲突，进入 Review Queue；批量提交删除未采用候选但保留证据与记录。
10. 用户编辑 Knowledge Page，SQLite 产生 User Revision，Markdown 重新物化。
11. 有任务时关闭窗口，应用进入托盘；明确退出并重启后从检查点继续。
12. `raw/` 文件缺失或散列错误时，文档被隔离且系统不声称可以自动重建。
13. 分别导入 TXT、Markdown、DOCX、XLS/XLSX、PDF、PPTX，Document IR 保留该格式可用的
    标题、表格、图片和页/slide/sheet/cell 来源坐标，并可从回答引用打开。
14. 扫描 PDF 自动进入增强解析并在 CPU 上产生带页码/bbox 的文本、表格和图片；整个过程
    不下载模型。用户也可以在失败文档恢复入口手动选择增强解析。
15. 旧 DOC/PPT 通过随包 Tika 低保真读取文本；Tika 空结果直接隔离并提示转换为 DOCX/PPTX。
    干净 Windows 环境无需预装 Java，应用退出后不得残留 Tika 进程。
16. 固定问题集分别验证 FTS/PageTree/Wiki 基线、增加局部图谱后的 Recall@K、引用精确率、
    忠实度、延迟和成本；图谱未证明增益时不得默认启用，系统不存在向量索引或 Embedding 调用。

## 12. 桌面技术验收

- 前端新增组件测试与 Desktop Bridge 合约测试，覆盖 Import Job 状态、隔离恢复、流式中断与
  原位替换、Answer Evidence、知识库切换和 Python Engine 重启；
- Bridge 测试用内存实现驱动 React，不依赖真实模型或打包进程；
- Python/Rust/TypeScript 对同一协议 fixture 执行编码、解码、版本拒绝和错误分类测试；
- 正式 Portable Desktop Package 在原生 Windows runner 上执行入口、固定 WebView2、拖放、
  托盘、通知、Engine 监管、安全退出和重启恢复端到端冒烟测试；
- Tauri 桌面 E2E 使用官方 WebDriverIO Tauri service 的独立测试构建，测试插件不得进入正式包；
  Playwright 只用于纯 React/browser-mode 测试，不能替代窗口、托盘、单实例和进程树验收；
- 最终 ZIP 必须在无 Python/Node/Rust、离线的干净 Windows 10 22H2 与 Windows 11 x64 环境
  验证中文/空格路径、固定 WebView2、关闭到托盘、任务继续和明确退出无残留；
- TypeScript 构建、ESLint、中英文键一致性、前端测试和未使用依赖检查进入常规 CI。
