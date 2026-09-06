# 当前设计决策索引

本索引区分当前约束、历史论证和未完成工程。实现状态以
[设计核对矩阵](reviews/2026-09-06-design-implementation-matrix.md)及其
[补齐记录](reviews/2026-09-06-remediation.md)为准；Issue 的开闭状态不单独证明功能完成。

| 主题 | 当前权威 | 适用边界 |
| --- | --- | --- |
| 领域语义与证据 | [ADR 0083](adr/0083-derive-domain-semantics-under-code-owned-evidence-constraints.md) | 模型决定开放语义；代码约束证据、代次、结构与资源。不恢复固定角色、领域词表、章节模板或关系本体。 |
| 候选、页面与图谱 | [ADR 0082](adr/0082-build-the-knowledge-graph-from-admitted-identities.md)及 ADR 0083 | 页面和关系共享不可变候选；可选图谱失败不阻塞页面。非字面跨文档判断须有证据绑定的模型或人工决定。 |
| 模型等待 | [ADR 0032](adr/0032-end-model-attempts-only-on-explicit-terminal-events.md)、[ADR 0037](adr/0037-observe-model-waits-without-timing-them-out.md) | 已建立请求等待真实终止或取消；检索墙钟预算只控制下一次派发。连接阶段仍有上限。 |
| 恢复解析 | [ADR 0022](adr/0022-normalize-format-specific-parsing-and-package-legacy-office-compatibility.md) | 普通恢复复用已验证结果；显式重新解析清除 DocumentIR 及后续检查点，仅影响该导入。 |
| Desktop 凭据 | [ADR 0023](adr/0023-store-model-configuration-in-knowledge-base-config.md) | KB 的 `.openkb/config.yaml` 存储配置与 API key，读取时掩码。仓库根 `.env` 仅用于开发评测。 |
| Runtime 与发布 | ADR 0083 | Runtime 验证结构和证据绑定，不宣称语义事实正确。多领域真实模型、人工 rubric 与当前 Windows 包验收共同构成发布证明。 |
| 旧 KB | ADR 0083 的 semantic epoch | 旧开发 epoch 在写入和模型派发前拒绝；不恢复旧语义迁移或旧图谱隐式回退。 |

仍需独立工程设计与实现的项目：

- [ADR 0019](adr/0019-generate-one-versioned-desktop-bridge.md)：单一 schema 生成三端契约及 revision Store；目前仍有手写类型和按事件刷新。
- [ADR 0060](adr/0060-bound-incremental-corpus-synthesis-without-truncation.md)：本轮补上队列合并和精确页面计划复用；整次 corpus 的 token/call 上限、预算暂停与继续仍未闭环。
- [ADR 0064](adr/0064-preview-and-preserve-reanalysis-generations.md)：完整预览、成本估计、变更报告和可操作的回滚入口。保留历史 generation 不能代替这些能力。

历史设计保留原文，其固定本体、旧兼容语义和旧 benchmark runtime gate 不再作为当前实现要求。
PageIndex 的历史结论仍为 NOT PROMOTED；当前包的真实模型和 Windows 验收没有因代码修复而自动完成。
