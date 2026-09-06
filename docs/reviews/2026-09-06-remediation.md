# 2026-09-06 审查缺口补齐

本轮修改工作区代码，落实[原始审查](2026-09-06-design-implementation-audit.md)中的六项具体缺陷，
沿用已经完成的领域目录重组。没有提交、推送或修改 GitHub Issues；没有运行付费模型或伪造人工、Windows 验收结果。

| 项目 | 已实现 | 回归边界 |
| --- | --- | --- |
| F1 证据映射 | `analysis/evidence_binding.py` 一起映射 claim 与 applicability；registry 发布及读取再验证嵌套证据子集，损坏快照返回 dependency unavailable。 | 从实际导入、持久化到 D1 复用的来源归属。旧错误数据需显式重分析，不静默重写历史 generation。 |
| F2 页面调度 | 独立 Engine corpus worker；候选与图谱发布事务内排队；按文档 revision 合并工作，迟到完成不消费新任务。图谱成功只产生增量通知。 | 无图谱执行也可生成页面；队列 revision 竞争；KB 切换及模型设置退役边界。 |
| F2 增量复用 | 精确绑定候选、claim、Evidence、关系、语言、prompt 和 execution profile 的页面规划缓存；复用时重新派生当前 generation 的引用并校验。 | 未变化输入不再调用模型，模型变化必须重新规划；缓存不能跨新候选代次套用。 |
| F3 等待语义 | 删除 response deadline/response timeout，provider read/write/pool 等待不受导航总时长取消；派发前单独检查预算。 | 已发请求跨越预算后仍接收有效结果；预算耗尽后不能开始下一操作或 repair。真实取消仍有效。 |
| F4 语义审核 | 新模型 claim compatibility 契约，原文证据约束与一次修复；冲突/未知保留当前页面。Python service、Engine、Rust、TS、Review UI 组成完整查询/决定路径。 | 冲突保留、人工接受后重新综合、代次变化拒绝旧决定、独立身份决定复用。多已有身份的合并暂不提供，只允许保留当前页面。 |
| F5 发布绑定 | 全生产代码树与打包输入摘要；十案例、三次重复和三类 metamorphic pairs；candidate 模式包含生产服务完整链路，禁止预置候选跳过准入。Windows smoke v2 要求十三项检查。 | 生产模块增删改都改变摘要；开发片段评测和不完整全链路记录不能签署；签署继续验证 package、matrix、output 与人工 rubric。 |
| F6 恢复解析 | Python/Rust/TS 与失败文档界面接通 auto/fast/enhanced；显式重解析清空派生检查点和分析计划，保留已验证 raw；选择持久化到该 job 的 recovery run。 | 模型阶段失败后切换解析模式确实重建 IR/证据；删除外部源文件后仍使用保存原文；新服务实例保留选择；非法 wire 输入被拒绝。 |

新增 schema migration 65–68 均为当前 semantic epoch 内的增量结构；没有改变旧 epoch 的拒绝策略。
构建依赖固定为此前缓存可用的 Hatchling 1.32.0 与 hatch-vcs 0.5.0，`uv lock --check` 通过。
README 已纠正凭据和等待说明；旧固定 corpus runtime attestation 文档改为当前多领域发布流程；
identity graph 设计标明被 ADR 0083 替代的条款。[当前决策索引](../current-decisions.md)保留未完成工程清单。

## 验证记录

| 检查 | 结果 |
| --- | --- |
| Python 全量回归 | `pytest -q`：750 passed，160.58 秒。随后完善评测报告字段及页面正文保存，对应文件 9 项回归全部通过。 |
| Python 静态检查 | Ruff lint 与格式检查通过；mypy 检查 315 个源码文件通过。 |
| Rust | `cargo fmt --check` 通过；`cargo test --quiet`：59 passed。 |
| 前端 | i18n 检查、TypeScript 与 Vite 构建、ESLint、UI 测试通过。 |
| 依赖与打包 | `uv lock --check`、wheel 构建通过；在源码目录外导入 wheel 的 314 个子模块，并完成 Engine 二进制帧握手。 |
| 文档与工作区 | 本轮四份主要文档的 16 个本地链接可解析；`git diff --check` 通过。 |

测试中的模型客户端均为模拟实现；完整链路测试运行真实导入、SQLite、候选、图谱、页面、
检索、回答及重启读取服务，但不证明模型的真实语义质量。产物保存实际页面正文、回答与引用，
rubric 补充候选及关系质量、跨文档一致性、回答支持程度，供真实模型评测后的人工核对使用。

## 待独立完成的工程与验收

- ADR 0019 的统一 schema 生成与 revision Store。
- ADR 0060 的整次 corpus 调用/token 预算及暂停继续。
- ADR 0064 的重分析预览、变更报告和回滚；现有 reanalysis 完成状态与后续页面综合状态仍需进一步统一。
- 当前源码与候选包的真实模型矩阵、人工作品级 rubric、签署以及 clean Windows 10/11 完整验收。

旧 `.semantic-eval` 报告仅是旧实现的历史证据，不能用于签署本轮实现。当前代码质量检查与上述
发布验收分开记录；保持 #31/#115/#116 的待验收事实，不把本地测试通过换算成 release ready。
