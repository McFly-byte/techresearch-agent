# Core10 质量诊断：低分主要来自证据覆盖，而非 Judge 解析

## 技术摘要

固定 Core10 v7 已完成 10/10、失败 0，平均 Qwen 非官方 Judge 分数为 `0.074584`，平均耗时 `233.78s`，平均 Token `44,664.9`。逐题重算证明本地 Judge 公式、rubric 数量、三态分母和 `pass_rate` 阈值均按代码执行；10 题不存在 `parse_error`、`judge_timeout` 或默认零分。`pass_rate=0%` 的直接原因是最高单题分数只有 `0.176471`，没有题达到 `0.5` 阈值。

主要质量问题发生在 Judge 之前：8/10 个长问题的四个 Worker query 经 1500 字符截断后完全相同；fact extractor 没收到子任务范围；写作前只验证前 8 条事实，却要回答每题 40–91 条 rubric。结果呈现出明显的“结构完整、事实稀疏”：presentation rubric 通过 25/52（48.08%），info_recall 仅通过 14/433（3.23%），analysis 仅通过 5/127（3.94%）。

另有一个独立的评测展示问题：本地 summary 按 qid 只保留最终结果，而 v7 LangSmith Experiment 保留 15 个根 run（10 个最终完成和 5 个历史失败尝试）。最终完成 run 的分数、Token、run_id、trace_id 与本地逐条一致，但若 LangSmith 按全部 15 个 run 聚合，均分约为 `0.04972`，与本地 `0.074584` 不一致。因此评分器本身正确，重试后的 Experiment 聚合口径需要最终状态快照。

## 范围、数据与指标定义

- 基线目录：`evals/runs/drb2_core10_live_qwen_v7`
- 固定子集：`evals/subsets/drb2_core10.json`
- 数据集 SHA-256：`2b29012d0842b2986fa532bebb67aa9362fe1fe5cee81a63a70c6f1f30f9ff69`
- 语言与主题：中文 5、英文 5，10 个不同主题。
- Judge：`qwen-nonofficial`；`official_judge = not_run (requires GPT-5.5)`。
- Judge score：非 blocked rubric 中 `score=1` 的数量除以非 blocked rubric 总数。本批次没有 blocked rubric。
- pass_rate：全部题中 `judge_score >= 0.5` 的比例；failed 题也留在分母。本批次 failed=0。
- Token：Qwen API 返回的 Research、Verifier、Synthesis、Judge 使用量之和，不含 Tavily 搜索计费，也不展示 cost 指标。
- “结构可回溯引用”：结果中的 URL 为合法 HTTP(S) locator，且生成质量门禁确认正文 `[cN]` 均能映射到当次 synthesis citation map。它不等价于“引用足以支持 benchmark rubric”。
- “引用利用率”：正文中不同 `[cN]` 数量 / 结果中保存的候选 URL 数量，仅用于识别证据在 synthesis 中的损失。由于结果只保存 URL、不保存原 citation_id 到重编号 `[cN]` 的映射，该值是保守的结构诊断，不是语义 precision。

Core10 的题长与 Full132 接近（平均 2,064 vs 2,132 字符；中位数 1,810 vs 1,855），语言完全均衡；但 rubric 数量略轻（平均 61.2 vs 71.3，中位数 58.5 vs 65.5），只覆盖 22 个主题中的 10 个。因此它适合快速回归和发现系统性缺陷，不足以替代 Full132 最终结论。

## 评分链路审计

| 审计项 | 结论 | 证据 |
|---|---|---|
| Judge 公式 | 正确 | 逐题重算 `passed/live` 与保存的 `judge_score` 完全一致。 |
| pass_rate | 正确但阈值较高 | 代码阈值为 `0.5`，10 题最高 `0.176471`，因此 0%。 |
| rubric passed/total | 正确 | 10 题保存明细数与数据集 rubric 数逐题一致，共 612 条。 |
| blocked 分母 | 正确 | `-1` 不进入分子或分母；本批次 blocked=0。 |
| 解析/默认零分 | 未发现 | `parse_error=0`、`judge_timeout=0`，没有明细长度缺失。 |
| failed 与低分 | 本地未混淆 | 本批次 failed=0；`compute_metrics` 会让 failed 留在总分母但不伪装成 completed。 |
| resume attempt | 本地最终指标未污染 | 结果文件按 qid 原子覆盖最终状态，attempt 分布为 1×6、2×3、3×1。 |
| LangSmith attempt | 存在展示污染 | 同一 Experiment 有 15 个根 run；5 个历史失败 run 带 0 分 feedback。 |
| Qwen Judge 结构变体 | 本批次解析正常 | 612/612 明细存在，无 parse_error；解析器已覆盖数组、包装对象、NDJSON 和截断数组。 |
| Token 覆盖 | 完整 | 10/10 非零，四阶段合计 446,649，与 summary 一致。 |
| trace 覆盖 | 完整 | 10/10 有 run_id、trace_id、trace_url；最终完成 run 与本地逐条一致。 |

阶段 Token 构成为 Research 250,935（56.18%）、Judge 134,836（30.19%）、Verifier 31,294（7.01%）、Synthesis 29,584（6.62%）。Token 与耗时相关系数为 `r=0.805`，但分数与 Token 仅 `r=0.181`、与耗时仅 `r=0.155`。样本只有 10 题，这些相关性只能说明“多花 Token 没有稳定换来覆盖”，不能用于因果推断。

## 每题诊断

所有题的结构可回溯率均为 100%；“利用”列显示正文实际使用的不同 citation tag / 保存的 URL 数量。

| qid | score | rubric 通过 | latency | Token | rounds | attempt | 引用（可回溯；利用） | 主要失分与最可能根因 | 可修改模块；预期收益；风险 |
|---|---:|---:|---:|---:|---:|---:|---|---|---|
| task7 | 0.0345 | 2/58 | 327.0s | 72,310 | 4 | 1 | 7（100%；2/7） | 土地财政定义、年份、金额和税制表缺失；四个 query 截断后相同，前 8 facts 无法覆盖两大部分 | planner/worker/report；提高数值与政策覆盖；更多 verifier Token |
| task8 | 0.0192 | 1/52 | 200.4s | 33,115 | 4 | 1 | 7（100%；3/7） | 只保留两部分外形，算法与数据库属性大量漏答；四个 query 完全相同 | planner/extractor；方法与数据库分路取证；短 query 可能需保留主题锚点 |
| task17 | 0.1053 | 6/57 | 300.7s | 81,453 | 4 | 1 | 7（100%；4/7） | 技术条目有部分命中，应用、平台、优势覆盖不全；高 Research Token 无质量回报 | planner/worker；四类 checklist 定向检索；行业术语可能跨类重复 |
| task71 | 0.1765 | 12/68 | 291.2s | 41,485 | 4 | 1 | 10（100%；2/10） | 主题分析框架较好，但研究表中的具体论文、工具和发现缺失；保存答案还达到 8,000 字符截断上限 | planner/synthesis/storage；按论文表与三类分析分配证据；输出变长风险 |
| task25 | 0.1250 | 8/64 | 234.2s | 47,149 | 4 | 1 | 5（100%；2/5） | 总论和少数机制命中，六类干预策略的药物、机制、效果不全；通用 extractor 未按子任务抽取 | planner/extractor；六策略分组检索并保留机制证据；医学来源验证更慢 |
| task32 | 0.0172 | 1/58 | 159.8s | 19,733 | 4 | 1 | 10（100%；3/10） | 报告明确写“Data Not Available”，工厂清单、选址因素、时期比较几乎为空 | planner/extractor；三项要求分别检索；历史专名检索召回仍不确定 |
| task84 | 0.0000 | 0/91 | 280.6s | 42,117 | 4 | 2 | 22（100%；2/22） | 候选来源最多但最终拒答，六种 SA 的机制、元件、信号、限制全丢；明显是 evidence→synthesis 断层 | extractor/report；按结构建立矩阵并跨 task 选 facts；专业缩写需精确匹配 |
| task40 | 0.1250 | 5/40 | 193.6s | 35,827 | 4 | 2 | 11（100%；3/11） | 命中部分时间线与“语义转换”，但早期法规、条件、人物陈述不全 | planner/report；时间线与争议分析分路；敏感主题来源质量需谨慎 |
| task47 | 0.0508 | 3/59 | 177.9s | 34,608 | 4 | 3 | 10（100%；2/10） | 表格结构完全命中，但阈值、正负方向与活动差异多数为“无数据” | planner/extractor；按活动组和因素检索；要求多于单轮可覆盖量 |
| task48 | 0.0923 | 6/65 | 172.4s | 38,852 | 4 | 2 | 12（100%；1/12） | 四段结构正确，推荐量、RCT 和骨健康研究细节缺失；候选来源未进入正文 | extractor/report；按四节分配 evidence；RCT 细节可能增加输入长度 |

## 共性根因排名

### 1. Search query 实际重复，检索覆盖被系统性压扁

旧 planner 把完整问题放在最前、把 `architecture/ecosystem/trade-offs/production` 后缀放在末尾。Core10 中 8/10 题超过 1,500 字符，Worker 截断后四个后缀全部消失，四个 Worker 实际提交相同 query。这个结论可由 planner 和 Worker 的确定性代码直接复现，不依赖 LLM 判断。

### 2. Fact extraction 与子任务脱节

Worker 搜索时知道 `current_query`，但调用 extractor 时只传顶层 `user_context`；评测的 `user_context` 为空。LLMFactExtractor 因而只看到网页正文和通用“抽取具体事实”指令，不知道该找哪一个问题要求。它可能返回真实、可引用、但与 rubric 无关的事实。

### 3. Evidence 到答案的硬截断和顺序偏置

VerifiedReportBuilder 旧逻辑仅取 `facts_to_claims(facts)[:8]`。并发 facts 按 fact_id 排序，前 8 条可能集中来自较早 task；随后 synthesis 即使声明上限 15，也只能拿到这 8 条。每题 40–91 条 rubric 与 8 条证据之间存在结构性容量错配。101 个保存 URL 中，正文只出现 24 个不同 citation tag（约 23.8% 的候选引用利用率），task84 为 2/22、task48 为 1/12，说明 synthesis 丢失非常明显。

### 4. LangSmith Experiment 混合最终结果和历史失败尝试

这是测量层根因，不解释 Agent 低分，但会让 UI 均值进一步降低。应保留 attempt 历史用于审计，同时另建零模型调用的 final-state snapshot，避免把“运行可靠性失败”与“最终答案质量”混成一个聚合值。

## 最小改进方案

### P0：question-derived coverage planning 与 query diversification

- 修改 `src/agents/planner.py`、`src/graph/state.py`、`src/agents/worker.py`。
- 从用户问题可见的编号、分部和 bullet 确定性提取 checklist；不读取 rubric/reference。
- 每个 SubTask 保存 requirement_ids、coverage_requirements、短 search_query。
- 将区分性 requirement 放在 query 前部，长度上限 800，避免后缀被截断。
- extractor 同时收到当前 SubTask 的 coverage scope。
- 预期收益：提高 info_recall；减少重复搜索和低收益 Research Token。
- 风险：无显式列表的开放问题会退回通用 planner；短 query 可能损失上下文，因此保留 topic anchor。

### P0：coverage-aware evidence selection 与 synthesis

- 修改 `src/service/verified_report.py`、`src/api/runner.py`、`src/api/task_store.py`、eval 结果模型。
- 在不同 source_task_id 间轮询选事实，去除重复 claim，将验证上限从 8 提高到 16，并保持 verifier 并发 2。
- 将 question-derived checklist 和 claim coverage 标签放入 synthesis constraints。
- 持久化 requirement→task→query→fact→citation→claim→verified 的 coverage matrix。
- synthesis 输出预算从 2,048 提高到 3,072，避免长表格/多节报告被过早截断。
- 预期收益：降低 Worker 有证据但最终漏答的比例；使缺口可观测。
- 风险：Verifier 与 Synthesis Token 会增加；必须用 Core4 验证总 Token 不超过基线的 110%。

### P1：LangSmith final-state snapshot

- 修改 `evals/runner.py`。
- attempt>1 时，从原子保存的最终 JSON 创建独立 snapshot Experiment；target 仅回放缓存字段，不调用研究、搜索、synthesis 或 Judge。
- snapshot 每个 qid 恰好一行，并保留 source_trace_id/source_trace_url 指向真实研究 trace。
- 预期收益：LangSmith 与本地 summary 可直接对齐，同时保留原 Experiment 的完整 attempt 历史。
- 风险：会多一个 Experiment；文档必须区分“attempt history”和“authoritative final snapshot”。

## 已验证事实、待验证假设与限制

数据直接支持：评分公式正确；0% pass_rate 不是解析 bug；15 个 LangSmith 根 run 污染 UI 聚合；8/10 题的旧 query 完全碰撞；extractor 未收到 task scope；验证硬上限为 8；三维度通过率差异；Token 与耗时强相关但与分数弱相关。

待 Core4 验证：coverage-aware query 是否能提高有效来源召回；16 条均衡证据是否足以提升 info_recall；Research Token 是否因减少重复而抵消新增 verifier/synthesis Token；Qwen Judge 的相对变化是否在固定题上稳定。Qwen Judge 始终只能标为非官方预评，不能外推为官方 DRB2 成绩。

## 验证计划

1. 离线：全量 pytest、Ruff、mypy；定向检查长问题 query 唯一性、extractor scope、coverage matrix 序列化、final snapshot 零模型调用。
2. Core4：固定采用 Core10 前 4 个 qid（task7、task8、task17、task71），新目录、新 Experiment，q2/w2、900s 硬超时。
3. 与 v7 同 qid 基线比较 score、Token、latency、维度通过数、trace/usage/coverage 完整性。
4. 只有 Core4 无系统性失败且质量/效率取舍可接受时，才运行干净 Core10；绝不复用 v7 目录。

## 面试中如何讲解

三分钟版本：项目最初已经解决“跑不完”和“不可观测”，但固定 Core10 的完成率 100% 并不等于研究质量高。我先重算 612 条 rubric，排除 Judge 解析和本地聚合错误，再把 trace、Token 和代码控制流对齐。证据显示 presentation 约 48% 通过，但事实和分析只有约 3%–4%；进一步发现长问题让四个 Worker 实际发出相同 query、extractor 不知道子任务、writer 只看前 8 条事实。于是没有增加 Agent 数量，而是增加一个问题派生的 coverage contract，贯穿 requirement、query、evidence 和 answer，并用轮询选择避免并发顺序偏置。同时把重试历史与最终质量 Experiment 分开。最后用固定 Core4 做同题对照，只有真实提升且 Token/延迟受控才扩大到 Core10/Full132。

面试追问重点：为什么不能把 rubric 给 Agent（会污染 benchmark）；为什么 deterministic checklist 比新增 Planner LLM 更合适（可解释、零额外调用、先验证最小方案）；为什么提高 verifier 上限仍可能节省总 Token（旧 Research 重复占 56%）；为什么保留原 attempt Experiment（运行可靠性审计）又新增 final snapshot（最终质量口径）。
