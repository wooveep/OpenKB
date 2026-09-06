# OpenKB 设计、实现与 GitHub Issues 核对 — 2026-09-06

> 本文保留修复前的审查快照。六项实现缺陷的修复、当前验证和仍待验收事项见[补齐记录](2026-09-06-remediation.md)。下文的代码行号和“当前”均指审查时状态。

当前实现已经覆盖 Desktop、可恢复导入、版本目录、证据检索、动态语义规划和原子 generation 的主要结构。目录重组改善了代码定位，但没有完成全部设计承诺：证据映射、可选图谱与页面综合的调度、模型等待终止、知识审核闭环和发布证明仍有实质缺口。不能把开放 Issue 数量当作剩余功能数量，也不能把现有测试通过当作真实语义质量或 Windows 发布验收通过。

本报告针对 `b243f61` 基线之上的当前工作区，包括同次审查中已完成但尚未提交的目录重组。核对范围为 79 份 ADR、7 份本地设计文档、README、CONTEXT、开发规则、检索评测与发布文档，以及 [wooveep/OpenKB](https://github.com/wooveep/OpenKB/issues) 的全部 116 条 Issues（正文及可获得的评论；48 OPEN、68 CLOSED）。研究笔记作为设计背景，历史评论中的测试和 Windows 结果作为当时证据；本轮没有重新执行所有历史验收，也没有运行付费模型或 Windows GUI。

逐 ADR、逐 Issue 记录见[核对矩阵](2026-09-06-design-implementation-matrix.md)。目录重组、公共模块提取和完整测试记录见[架构审查](2026-09-06-architecture-review.md)与[当前目录地图](../architecture.md)。本报告记录审查时事实；没有修改远端 Issues 或运行时行为。

## 应按哪份设计判断

文档不是同时有效的功能清单。应按明确的替代关系裁决，保留没有被替代的不变量。

| 主题 | 当前有效要求 | 不应继续当作缺陷的历史差异 |
| --- | --- | --- |
| 领域语义 | [ADR 0083](../adr/0083-derive-domain-semantics-under-code-owned-evidence-constraints.md)、[Spec #101](https://github.com/wooveep/OpenKB/issues/101)，以及 [#100 的 2026-09-05 修订](https://github.com/wooveep/OpenKB/issues/100) | 固定 answer kinds/aspects、claim roles、按 kind 固定选择的章节标题和布局、relation ontology、代码词表准入均已被替代；不能建议恢复。 |
| 版本与证据 | #100 的 Lineage、Catalog/Diff、Scope、Occurrence、Citation、snapshot 与原子发布要求继续有效 | 新语义设计没有免除版本隔离、证据归属或未决冲突审核。 |
| Runtime 与 release | Runtime 只做 Corpus Generation Integrity Gate；release 做多领域真实模型与人工签署 | ADR 0065 和旧 OCloudware runtime 质量门已被 ADR 0083 替代。 |
| 旧 KB | ADR 0083 要求新 semantic schema epoch，旧开发 KB 明确拒绝、零模型调用、无静默数据改写 | 不应依据旧 ADR 0050/0082 要求恢复被替换语义的兼容 reader 或 graph fallback。通用锁、备份及非破坏性要求仍有效。 |
| 凭据 | [ADR 0023](../adr/0023-store-model-configuration-in-knowledge-base-config.md)：Desktop 使用 KB 配置内的模型与 API key；评测单独使用根目录忽略的 `.env` | ADR 0018 的环境变量凭据引用已改变；README 尚未同步。 |
| 等待与预算 | [ADR 0032](../adr/0032-end-model-attempts-only-on-explicit-terminal-events.md)、[0037](../adr/0037-observe-model-waits-without-timing-them-out.md)：已建立 model attempt 只由显式终止事件结束；ADR 0081 的 120 秒预算在操作之间检查 | 旧递增 response timeout 和总截止时间不是当前默认策略。 |
| Windows 与 PageIndex | #31 保留最终 clean-machine 验收；[#63](https://github.com/wooveep/OpenKB/issues/63) 的历史结论是 **NOT PROMOTED** | #63 关闭不表示 PageIndex 通过全部质量门或已默认启用，也不表示当前包已发布就绪。 |

## 需要修复的实现缺口

P1 表示下一轮优先修复或发布前必须解决，包含证据完整性与发布证明的阻断项，不表示已发生线上数据损坏或错误发布。P2 表示应补齐的功能、规范或可操作性缺口。

### F1 · P1：Applicability 的证据 ID 没有随 claim 一起 canonicalize

要求：ADR 0083 第 102–104 行规定每个 applicability entry 的证据必须是所属 claim 证据的子集；[#103](https://github.com/wooveep/OpenKB/issues/103) 与 #100 同样保留这一约束。

实现：[candidate_persistence.py](../../openkb/knowledge/corpus/candidate_persistence.py) 第 29 行转换 `claim.source_evidence_ids`，第 71 行却直接序列化 `entry.as_dict()`。嵌套的 `source_evidence_ids` 保留模型输入的临时 ID。[reuse.py](../../openkb/knowledge/analysis/reuse.py) 的 `canonical_analysis_evidence_map_in` 明确负责临时 ID 到当前文档 canonical Evidence 的映射；registry 快照又复制已持久化的 applicability，因此错误会越过第一层持久化。

已用内存 SQLite 和合法候选复现，不访问模型或用户 KB：

```json
{"persisted_claim_sources":["canonical-evidence"],"persisted_applicability_sources":["prompt-evidence"],"subset_valid":false}
```

影响：当导入或 D2/复用路径发生非恒等 Evidence ID 映射时，会持久化不满足所属 claim 来源子集约束的 applicability 引用。本轮没有据此证明最终回答已发生错误引用。修复应在同一证据映射接口中转换所有嵌套引用，并在 registry 写入前再次检查子集关系；回归应穿过 import/reuse → persistence → registry → page snapshot。

### F2 · P1：可选图谱失败会阻止正常导入的页面规划

要求：ADR 0083 第 70–81 行将页面规划定义为独立操作，并允许关系放置为空；[#109](https://github.com/wooveep/OpenKB/issues/109) 明确零 relation placement 合法。结合 ADR 0054 的可选图谱定位与 ADR 0060 的自动页面综合要求，页面规划不应以图谱发布成功为前提。

实现：[graph/tasks.py](../../openkb/knowledge/graph/tasks.py) 第 359–365 行仅在 `published_claim` 为真时调用 `CorpusKnowledgeSynthesisPipeline.run_generation(gateway=...)`。图谱合约 suspended 在第 328–341 行提前返回，抽取失败在第 371–374 行返回。正式导入路径没有对应的独立页面调度补偿；显式 reanalysis 是另一条入口。

影响：Knowledge Analysis/candidate 已成功、page planner 可用，但 graph operation 被暂停或失败时，新导入知识仍可能缺少生成页面。现有 pipeline 的“无关系也可渲染”单元行为不能覆盖这一入口调度缺口。

建议由 corpus synthesis 所属调度器推进候选到页面发布；图谱结果作为带状态的可选输入。保留 candidate generation pinning、迟到结果 supersession 与原子 activation，增加“正常导入 + 仅 graph operation 失败”的集成回归。

### F3 · P1：导航预算重新变成了已建立模型请求的响应截止时间

要求：ADR 0032 第 3–9 行禁止已建立 attempt 的 read/total deadline；[ADR 0081](../adr/0081-use-bounded-adaptive-knowledge-navigation.md) 第 23–25 行明确 120 秒在操作之间检查。[#74](https://github.com/wooveep/OpenKB/issues/74)、[#85](https://github.com/wooveep/OpenKB/issues/85) 因而不能仅按已有 terminal gateway 判为完成。后台 enrichment/graph 的默认 terminal 路径没有在本轮发现同一 deadline。

实现：[retrieval/service.py](../../openkb/retrieval/service.py) 第 137–163 行把 wall deadline 同时塞入 `is_cancelled` 和 `response_deadline`；[response_wait.py](../../openkb/models/response_wait.py) 第 42–48 行到期即抛出 `DesktopModelCancelledError`。transport 也会设置对应 read timeout。

无网络阻塞 provider 复现：请求设置 0.05 秒 response timeout，没有用户取消、没有 provider terminal event，仍产生 `queued → connecting → awaiting_model_result → cancelled`，并抛 `DesktopModelCancelledError`。

建议把“是否允许下一项导航操作”和“当前 model attempt 是否结束”分开；预算耗尽用 `budget_exhausted` 表达，不冒充用户取消。若产品确实要更改等待策略，应先明确修订 ADR，而不是让调用方参数悄悄覆盖它。

### F4 · P2：知识冲突审核只有部分存储，没有完整判断与裁决闭环

要求：[ADR 0062](../adr/0062-defer-unresolved-clusters-without-blocking-safe-knowledge.md)、[0063](../adr/0063-review-knowledge-with-evidence-and-reusable-decisions.md) 和 ADR 0083 第 115–119 行要求非字面冲突由 evidence-bound 模型判断或人工审核，未决项进入 review；身份与 claim 决策应能持久复用。

实现：[corpus/knowledge.py](../../openkb/knowledge/corpus/knowledge.py) 第 339–343 行保留 conflict 分支，但 `_claim_conflicts`（第 473–476 行）恒返回 `False`，未发现替代的模型判断持久化路径。不能据此判定任意两个不同事实互相冲突，但可以确定这一“冲突送审”分支不可达。

身份多匹配能写入 `knowledge_identity_review_items`（第 522–541 行），但生产代码对该表只有建表与插入，没有列表读取或裁决更新。现有 Review UI 对应旧 reconciliation / missing source，不提供 ADR 0063 的 merge、keep separate、alias、change kind、applicability 等专门决策。

建议先定义一个证据化 Review 服务，集中 query、decision、reuse signature、generation 失效与恢复规则，再接 Engine/Rust/UI。不要增加基于否定词、role 或领域关键词的冲突启发式。

### F5 · P1：发布签署绑定范围不足以证明 exact implementation 与完整 Windows 流程

要求：[#116](https://github.com/wooveep/OpenKB/issues/116) 要求最终 attestation 绑定 exact implementation，并覆盖 packaged import → candidate/page/graph → query/answer → restart、能力失败降级、obsolete KB 拒绝和隐私。

实现：[runner.py](../../evaluation/semantic_quality/runner.py) 第 625–655 行的 `_implementation_digest` 只覆盖 13 个手列文件，未包含模型 transport/gateway、candidate 持久化、graph、import/retrieval orchestration、Rust 或前端。已在临时源码副本中验证：替换 `openkb/models/transport.py` 为完全不同的实现，摘要保持不变。这个摘要可以标识所列 planner 文件，不能标识整个被验收实现。

[attestation.py](../../evaluation/semantic_quality/attestation.py) 第 17–24 行的 Windows smoke 检查集合仅有 package、import、query/page planning、version comparison、citation 六项；校验还要求集合完全相等。它没有表达 #116 的 candidate/graph synthesis、grounded answer、restart、failure degradation、obsolete KB rejection 或 privacy 结果。绑定 package SHA 能识别包字节，但不能补足未执行或未记录的验收项。

建议从统一、可审计的生产源码/构建输入清单生成摘要；扩展 smoke 证据合同，并由真实 Windows runner 产生。当前签署流程正确拒绝缺包、缺 smoke 或未通过的确定性结果，应保留这些限制。

### F6 · P2：桌面恢复流程没有接通 parser mode override

要求：[ADR 0022](../adr/0022-normalize-format-specific-parsing-and-package-legacy-office-compatibility.md) 第 23–27 行要求用户能在恢复时强制 fast/enhanced。

实现：Engine [imports.py](../../openkb/engine/imports.py) 能接收顶层 `parser_mode`，parser 自身也支持；但正式 Rust recover 请求只发送 `job_id` 与 `recovery_override`，Python/Rust/TS 的 DesktopRecoveryOverride 没有 parser mode，前端恢复面板没有该选项。因此桌面恢复总走默认 `auto`。这是跨层接线缺口，不是 parser 能力缺失。

另外，[ADR 0060](../adr/0060-bound-incremental-corpus-synthesis-without-truncation.md) 的增量成本控制和 [ADR 0064](../adr/0064-preview-and-preserve-reanalysis-generations.md) 的预览、变更报告、回滚体验也仅部分实现。当前每份图谱成功后以 `force_generation=True` 发起综合，没有传 `affected_document_ids`，planner 遍历该代全部身份；缺少所承诺的受影响项合并调度、整次调用/token 上限及预算暂停后的继续执行。reanalysis 任务还会在页面综合之前记为 completed，随后页面失败主要进入日志。下一步抽取 corpus 调度模块时，应同时收敛重复综合、每页 defer/carry-forward 结果和任务最终状态；保留历史 generation 本身不等价于可操作的回滚入口。

## 文档与规范维护缺口

| 问题 | 证据与影响 | 建议 |
| --- | --- | --- |
| README 的凭据与等待行为过时 | [README](../../README.md) 第 18–20 行仍要求环境变量引用；第 38–41 行仍描述递增 timeout 重试。与 ADR 0023/0032/0037 和当前导入行为不符。 | 用实际 Desktop settings 与 terminal-event 行为更新操作说明；F3 单独修复。 |
| 旧发布评测仍描述为当前门禁 | [desktop-retrieval-evaluation.md](../desktop-retrieval-evaluation.md) 第 127–166 行引用已不存在的 `openkb/benchmarks/real-corpus-attestation.json`，宣称固定套件的 115/117 结果和 runtime qualification gate。 | 保留历史结果但明确历史身份，链接到 ADR 0083 与当前 semantic evaluation；不可据此宣称新 epoch release ready。 |
| 详细设计缺少替代状态 | 09-04 identity-graph 设计仍写“已确认，作为 ADR 0082 的详细实施规范”，正文保留 ontology 与兼容迁移；另一份 corpus-aware 设计已经正确标记为历史。版本设计仍写 proposed，尽管 #100 与实现已有大量落地。 | 建立 current-decision 索引；给被替代的部分加定向指引，不重写历史论证。旧代码路径可保留为历史快照，由 architecture.md 指向新路径。 |
| ADR 0019 的生成合同与 revisioned Store 尚未落实 | [ADR 0019](../adr/0019-generate-one-versioned-desktop-bridge.md) 要求单一 schema 生成三端合同，并按 revision snapshot / sequenced event 驱动 Store；当前 Python/Rust/TS 各自维护类型，前端主要按 event kind 触发查询刷新。 | 明确继续实现这一方案或记录替代决策。现有手写校验和刷新工具不能等价于 schema 生成、乱序/重连快照协调。 |
| 文档 Git 白名单遗漏 | 上一轮将 `desgin` 改名为 `design` 时，`docs/.gitignore` 仍只允许旧目录；新报告与 architecture.md 也会被忽略。 | **本轮已修正**精确白名单。保留默认关闭策略和两份本地设计的忽略状态，未批量发布 maintainer-local 内容。 |

## 已有实现与验证证据

| 能力 | 当前对应实现 | 判断 |
| --- | --- | --- |
| 文档导入与恢复 | `importing/`、`parsers/`、`models/`，原文与状态由所属 service 和 KB 锁管理 | 成熟主链路；等待策略和 parser override 有上述缺口。 |
| 版本化检索 | [version_catalog.py](../../openkb/documents/version_catalog.py)、[version_diff.py](../../openkb/documents/version_diff.py)、[version_scope.py](../../openkb/documents/version_scope.py)、[scoped_evidence.py](../../openkb/retrieval/scoped_evidence.py) | 人工确认链、确定性 diff、统一 scope、D2 occurrence 投影已经存在；#100 的整个问题陈述不再代表当前缺失列表。 |
| 动态语义 | `models/semantic_structure_contracts.py`、`retrieval/query_planning.py`、`knowledge/pages/{planning,planner,page}.py` | 已有 seed-first query planning、动态 facets、页面 plan 验证与一次修复；graph/claim/review 边界仍须补齐。 |
| Generation 与 schema epoch | `knowledge/corpus/knowledge_pipeline.py`、`synthesis_generation.py`、workspace migrations | 已有固定输入、迟到结果拒绝、逐页 defer/carry-forward、完整性 activation 和旧 epoch 拒绝测试。 |
| 发布评测工具 | `evaluation/semantic_quality/` | runner、raw output digest、pending attestation、人审 rubric 与显式签署都存在；覆盖面及绑定合同不完整。 |

本轮重新运行 8 个版本/评测/领域独立性测试文件：**42 passed**；另运行 schema epoch、candidate registry、corpus pipeline 的聚焦回归：**14 passed**，合计 **56 passed**。测试使用本地 fixtures/provider doubles，没有新模型调用。F1 与 F3 用最小本地复现确认；F5 用临时副本验证摘要盲区。测试通过证明覆盖到的行为，不能抹除没有被测试覆盖的缺口。

上一轮当前重构的完整验证记录是 730 Python tests、57 Rust tests，以及 Ruff/mypy、前端 lint/tests/build、wheel 和独立目录启动检查；本轮仅修改审查文档和 docs 白名单，因此没有重复整套构建。

## GitHub 台账应该如何处理

1. **历史完成与当前设计分开。** #2–#70 中除 #31 外已关闭，但 #8 的旧 deadline、#27 的凭据引用、#25 的旧 graph 实现等已被后续 ADR 改写。#63 的关闭表示“不提升”决策完成；不要改写为全部验收成功。
2. **开放不等于未实现。** #72–#94、#102–#114 中有大量对应代码与测试。逐条按 matrix 补充当前实现、测试及尚缺验收项，再决定关闭、拆分或保留；本轮没有修改远端状态。
3. **#71/#85 不能整体关闭。** 仍有 F3 的响应截止时间；#78 的相关 parser recovery 接线也需要补齐。
4. **#97/#100/#101 仍是有缺口的总规格。** F1、F2、F4 不是文档过时，而是现行约束未形成闭环。旧 ontology / runtime benchmark / semantic fallback 的移除则是正确执行 ADR 0083。
5. **#115 已有局部实跑，仍不能关闭。** 本地 `candidate-20260906-domain-neutral-v2` 记录 8 cases × 3 repetitions × 2 operations = 48 logical operations，50 physical calls，0 invalid operations；raw-output SHA 与 report 一致。它只执行 query/page planning，case 输入预置 Evidence 和 claims，没有完整 candidate/relation 生成。中英配对满足至少一组的要求；仍缺 domain-word replacement/structural transformation 的 live 链路证明。没有可用的 maintainer-signed attestation，状态正确保持 `pending_human_review`。
6. **#31/#116 继续保持发布未验收。** 既有 #31 评论只证明历史固定包在提供的 Win11 主机上的部分成功，明确缺 clean offline Win10 完整记录；当前重构后两次本地评测的 implementation digest 均不匹配当前代码。必须用当前候选包补做完整验证与人工签署，不能复用旧结果宣称 ready。

## 对后续结构简化的建议

现有按领域组织的目录可以作为稳定起点。下一步应以以上实际耦合点提取公共行为，避免继续按文件大小机械拆分：

1. **证据映射接口**：统一处理 claim、applicability、summary、relation 的 ID 映射及 owning snapshot 验证，优先修复 F1。
2. **Corpus 调度接口**：candidate-ready、optional-relation-outcome、page planning、activation 由所属 pipeline 协调，移除 graph task 对页面进度的控制，修复 F2。
3. **Model attempt 与 session budget**：等待/取消由 model lifecycle 所属模块管理；retrieval 只决定下一步是否获准启动，修复 F3。
4. **Review 服务**：把身份与 claim 的可查询待审项、可复用决策与 generation 后果收进一个深模块，再接三端 UI，修复 F4。
5. **跨语言合同与 release evidence**：收敛 schema 来源和构建输入摘要，减少三份手写协议及不完整签署清单，修复 F5/F6 与 ADR 0019 偏差。

这些改动各自应以真实 service seam 的行为测试验收。发布与真实模型质量证明作为独立验收阶段，保持 pending 直到证据完整。
