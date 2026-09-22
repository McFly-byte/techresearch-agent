# Core10 第一轮质量改进报告

## 1. 问题背景

DeepResearch Agent 已经能在固定 Core10 上完成 10/10，并具备超时、并发、原子保存、resume、trace 和 Token 统计，但“能跑完”不等于“研究质量合格”。v7 的 Qwen 非官方 Judge 均分仅 `0.074584`、pass rate 为 0%，平均每题消耗 `44,664.9` Token。本轮目标是先排除评测错误，再用最小、可解释的改动提高问题要求到最终答案之间的证据覆盖。

本报告严格区分三类状态：离线行为已经验证；历史基线已经测量；改进后的在线质量尚未测量。Core4 因当前 Tavily 凭据全部被服务端拒绝而系统性失败，不能编造“提升百分比”。

## 2. 基线数据

| 指标 | Core10 v7 |
|---|---:|
| completed / failed | 10 / 0 |
| Qwen 非官方 Judge 均分 | 0.074584 |
| pass rate（阈值 0.5） | 0% |
| 平均 / 最长耗时 | 233.78s / 326.95s |
| 总 / 平均 Token | 446,649 / 44,664.9 |
| 平均搜索轮数 | 4.0 |
| trace / Token 覆盖 | 10/10 / 10/10 |

Judge 为 `qwen-nonofficial`；`official_judge = not_run (requires GPT-5.5)`。该结果不能称为官方 DRB2 分数。

## 3. 诊断证据与根因

评分链路审计重算了 10 题、612 条 rubric：保存的 score 都等于非 blocked rubric 中 passed/live，明细数量与数据集逐题一致，没有 parse error、judge timeout 或默认零分。最高题分仅 `0.176471`，因此 pass rate=0% 是真实阈值结果，不是汇总 bug。

三个主要质量根因如下：

1. **检索 query 碰撞。** 旧 planner 把长问题放在 query 前部、差异后缀放在末尾；Worker 截断 1,500 字符后，8/10 题的四个 Worker 实际 query 完全相同。
2. **抽取器不知道子任务。** Worker 把空的顶层 user_context 交给 fact extractor，没有传当前问题要求；真实但不相关的 facts 仍可能被保留。
3. **写作前证据被硬截断且有顺序偏置。** verifier 只接收排序后的前 8 条 facts，而每题有 40–91 条 rubric。保存的 101 个候选 URL 中，正文只使用 24 个不同 citation tag，表明 evidence→answer 丢失明显。

维度结果进一步支持该判断：presentation 通过 25/52（48.08%），info recall 仅 14/433（3.23%），analysis 仅 5/127（3.94%）。Token 与耗时相关 `r=0.805`，但与分数仅 `r=0.181`；这说明重复消耗没有稳定换来覆盖，但 10 题样本不足以作因果推断。

另一个测量问题是 v7 的原 LangSmith Experiment 混有 10 个最终 run 和 5 个历史失败 attempt。最终完成 run 与本地结果逐条一致，但全 run 聚合会把均分压到约 `0.04972`。这影响展示口径，不解释答案低分。

## 4. 最小设计方案

本轮只选择三个高收益点，不增加 Agent 数量，也不把 benchmark rubric 或参考答案泄漏给生成链路。

### 4.1 Question-derived coverage planning

Planner 只从用户可见问题的编号、分节和 bullet 提取 coverage checklist。每个 SubTask 持久化 `requirement_ids`、`coverage_requirements` 和最大 800 字符的 `search_query`，区分性要求置于 query 前部；Worker 将当前 coverage scope 交给 fact extractor。

控制流由“完整问题 + 易被截断的通用后缀”变为“问题要求 → 分组子任务 → 短而不同的 query → 定向事实抽取”。无显式结构的问题仍保留通用 fallback。

### 4.2 Coverage-aware evidence selection

VerifiedReportBuilder 在不同 `source_task_id` 间轮询、去重后选择最多 16 条事实，避免并发完成顺序决定最终证据。Synthesis 同时看到 checklist 和 coverage label，输出预算从 2,048 增至 3,072。新增可序列化 coverage matrix：

`requirement → task → query → fact → citation → claim → verified`

矩阵进入 API、task store 和 eval 结果，便于 resume 后恢复与 trace 对照。权衡是 verifier/synthesis Token 可能上升，必须由同题 Core4 判断重复 Research 的下降能否抵消它。

### 4.3 Final-state Experiment snapshot

若同一 qid 有多次 attempt，EvalRunner 从原子保存的最终 JSON 创建一个独立 LangSmith snapshot；target 只回放缓存字段，不调用搜索、模型或 Judge，并保留 source trace 链接。原 Experiment 继续承担可靠性审计，snapshot 承担最终质量聚合。

## 5. 核心代码路径

| 路径 | 责任 |
|---|---|
| `src/agents/planner.py` | 从可见问题提取 checklist，生成短且多样的 query |
| `src/graph/state.py` | 定义可序列化的 SubTask coverage 状态 |
| `src/agents/worker.py` | 执行显式 query，并把 coverage scope 传给 extractor |
| `src/service/verified_report.py` | 跨任务均衡选证据、验证、生成 coverage matrix 与报告 |
| `src/api/runner.py`、`src/api/task_store.py` | 返回并持久化 coverage matrix |
| `evals/runner.py` | 保存 coverage matrix，创建零模型调用 final snapshot |
| `src/tools/search_providers.py` | 认证快速熔断、TLS 瞬态分类、全局限流与每 Key 单飞锁 |

## 6. 离线验证

新增行为测试覆盖：长问题 query 唯一性、隐藏 rubric/reference 尾部不参与规划、extractor 收到子任务 scope、跨任务轮询选证据、coverage matrix 链路、final snapshot 不产生模型调用、空消息 `InvalidAPIKeyError` 的异常类型识别，以及“同 Key 串行、不同 Key 并行”的并发约束。

截至本报告提交前：

- 全量 pytest：555 passed in 18.33s（包含长问题引用修复、SDK TLS 瞬态错误分类、每 Key 单飞和熔断等待者错误传播测试）。
- 搜索提供方专项：12 passed。
- Ruff：通过。
- mypy：100 个源文件无问题。

fake 模式、resume、父子 trace 和 usage 隔离接口保持不变；新增字段都有默认值并可 JSON 序列化。

## 7. LangSmith 最终状态快照

v7 final-state snapshot：

`https://smith.langchain.com/o/4f508cdf-ba06-4214-b90a-689e3ebdc812/datasets/3107dfc1-7654-4bc9-95ed-9654ca558821/compare?selectedSessions=77971487-112d-4775-b9fd-7ed31eea01fe`

它包含 10 个唯一 qid、10 个 completed 和 10 个 source trace。反馈均分 `0.074580`，与本地精确值 `0.074584` 相差约 `0.0000044`，来源是 LangSmith 的四位小数反馈精度。

## 8. Core4 实验设置与结果

固定 Core4 为 Core10 顺序中的前四题：task7、task8、task17、task71。配置为 Qwen provider、Qwen 非官方 Judge、题目并发 2、题内 Worker 2、单题 900 秒、Research 420 秒、Write 180 秒、Judge 240 秒；使用新目录 `evals/runs/drb2_core4_coverage_v1` 和独立 Experiment。

Experiment：

`https://smith.langchain.com/o/4f508cdf-ba06-4214-b90a-689e3ebdc812/datasets/3107dfc1-7654-4bc9-95ed-9654ca558821/compare?selectedSessions=d92c57b7-eafa-438b-98e3-a3055d530c21`

结果是 completed=0、failed=4：task7 在 Research 420.19 秒超时；task8、task17、task71 分别在 312.84、331.58、228.06 秒后没有产生 facts/citations。因为没有完成题，summary 的有效完成题 Token 和 latency 聚合为 0；这不是“零成本”或“更快”。

Trace 中 64 次搜索调用有 39 次 permanent fetch/tool failure、24 次 transient network failure、1 次没有形成有效结果。最小真实探针确认本地配置的五个 Tavily Key 均被 SDK 判定为 `InvalidAPIKeyError`。检查过程没有输出 Key；只验证了数量、长度、前缀和唯一性。

发现系统性失败后已停止，没有 resume、没有启动 Core10。随后修复异常分类：即使 SDK 异常消息为空，也能依据异常类型轮换并熔断所有无效 Key，最终给出脱敏的 `ToolAuthenticationError`，不再重复进行数十次无收益搜索。

### 凭据更换后的继续验证

2026-09-22 更换了五个新 Key；替换过程只更新本地 `.env`，没有输出或提交密钥。五个 Key 的首次独立最小搜索均成功，项目封装层健康检查也成功。随后使用独立目录 `drb2_core4_coverage_v2` 继续固定 Core4：

- task7 完成：score=`0.07`、latency=`123.54s`、Token=`51,194`、4 rounds、trace 完整。
- task8 和 task71 在约 106 秒后因 `citations_traceable` 质量门禁失败；task17 在发现同类系统性失败后被主动中止。
- 诊断确定这不是搜索失败：LLM 生成了无法映射的 citation tag，确定性引用模板本应修复，但模板把 1,937/2,913 字符的原问题完整放入标题，继而被 prompt-echo 门禁拒绝，导致修复未生效。
- 已将引用修复版标题改为短、语言匹配且不复述问题的固定标题，并增加长问题回归测试；全量测试增至 551 项。

修复后以独立目录 `drb2_core4_coverage_v3` 再次从零运行，但在 28.84 秒内 4/4 retrieval failure：task7、task8 明确为 `search_auth_permission`，另两题因共享 Key 池被熔断而没有 facts/citations。逐 Key 复查表明，Key 1、2、3、5 已返回 Tavily 原始 `InvalidAPIKeyError: The account associated with this API key has been deactivated`；Key 4 连续返回 TLS `UNEXPECTED_EOF`，也无法通过健康门禁。代码同时补充了 SDK `SSLError`/连接错误的瞬态分类与有限重试，避免把 TLS 故障误记为永久抓取失败。故没有继续 Core10。

并发审计发现此前只有进程级总并发 4 和轮询选 Key，没有严格保证同一 Key 同时只被一个请求使用。现已为每个 Key 增加跨 Provider 实例共享的 `asyncio.Lock`：同一 Key 的整段重试过程串行，不同 Key 仍可并行；等待 Key 时不占用全局 Tavily 请求槽；获得锁后再次检查熔断状态，避免排队请求重用刚失效的 Key。熔断原因也保存在共享池中，使等待者继承同一脱敏 `auth/quota` 错误。离线并发测试测得每 Key 最大并发为 1、两 Key 总并发为 2，且三个并发等待者只调用失效 Key 一次。现有 trace 只能证明账户被停用，不能证明停用一定由并发导致；由于当前 Key 已失效，该假设仍需下一批健康 Key 做 Core4 v4 在线验证。

per-key 单飞提交 `53de2cb` 上的 Core4 v4 已实际重试，独立 Experiment 为 `f1ebf542-29ea-4fa6-925a-b5ef13613d6c`。运行在 11.86 秒内结束，结果仍为 completed=0、failed=4：task7、task8 分别在 5.99/5.56 秒触发 `search_auth_permission`，共享池随后熔断，task17、task71 没有产生 facts/citations。四题均未进入 LLM 阶段，因此 Token 为 0。该结果证明新锁已进入真实运行版本，但当前凭据在请求开始阶段已经无效；它不能验证锁对健康 Key 稳定性的收益，也不能支持继续 Core10。

v4 Experiment：`https://smith.langchain.com/o/4f508cdf-ba06-4214-b90a-689e3ebdc812/datasets/3107dfc1-7654-4bc9-95ed-9654ca558821/compare?selectedSessions=f1ebf542-29ea-4fa6-925a-b5ef13613d6c`。

v2/v3 Experiment：

- v2：`https://smith.langchain.com/o/4f508cdf-ba06-4214-b90a-689e3ebdc812/datasets/3107dfc1-7654-4bc9-95ed-9654ca558821/compare?selectedSessions=e60b547d-4978-49f5-8082-b4e020a16d21`（发现系统性引用门禁失败后主动中断）。
- v3：`https://smith.langchain.com/o/4f508cdf-ba06-4214-b90a-689e3ebdc812/datasets/3107dfc1-7654-4bc9-95ed-9654ca558821/compare?selectedSessions=1a2cd83f-b6a6-4bd1-9d4a-1d841df69b10`。

## 9. 修改前后对比

| 指标 | 修改前 Core10 v7 | 修改后 Core4 v1 | 可否比较 |
|---|---:|---:|---|
| completed / failed | 10 / 0 | 0 / 4 | 否，外部搜索认证失败 |
| Judge 均分 | 0.074584 | 无完成题 | 否 |
| pass rate | 0% | 无完成题 | 否 |
| 平均 Token | 44,664.9 | 无有效完成题统计 | 否 |
| 平均耗时 | 233.78s | 无有效完成题统计 | 否 |
| trace | 10/10 完整 | 有失败 trace | 仅可用于故障诊断 |

因此当前只能确认离线控制流和可观测性改进，不能声称质量、Token 或延迟已经提升。失败本身提供了一个工程结论：外部凭据状态必须进入 pilot 门禁，认证异常必须快速失败。

## 10. 失败案例与局限性

- 当前五个 Tavily Key 都无效，导致真实收益无法验证；这属于外部依赖阻塞，但已通过异常类型和 trace 证据定位。
- checklist 来自问题表面结构，不等价于隐藏 rubric。开放式、无编号问题可能仍需通用规划。
- 16 条证据依然不可能逐一覆盖 91 条细粒度 rubric；它是受控容量提升，不是完整保证。
- citation URL 可回溯不等价于语义支持充分；当前仍缺自动化的 claim-source entailment 指标。
- Qwen Judge 只能用于同口径快速迭代，最终对外成绩仍需要官方 Judge。
- Core10 只覆盖 22 个主题中的 10 个，不能替代 Full132。

## 11. 是否值得运行 Full132

当前不值得。第二批 Key 虽然首次最小探针 5/5 成功，但在真实并发 workload 后迅速出现四个账户停用和一个持续 TLS EOF，说明“单次健康检查成功”不足以作为 pilot 前置门禁。必须先取得来源可靠、可持续使用的 Tavily 凭据，再用新的独立目录和 Experiment 重跑相同四题。只有 Core4 无系统性 provider/search 失败、trace 与 Token 完整，并且同题质量提升或质量/效率取舍可接受，才运行干净 Core10。Core10 达到 10/10 completed、0 failed、trace/Token 10/10、最长小于 900 秒且本地与 LangSmith 对齐后，才讨论 Full132。

## 12. 三分钟面试讲解稿

这个项目第一阶段已经解决了 Deep Research 任务跑不完的问题：我做了分阶段超时、有限并发、原子保存、断点恢复和 LangSmith trace。但固定 Core10 虽然 100% 完成，Qwen 非官方 Judge 均分只有 0.0746，说明完成率不能代表研究质量。

我先没有改 Prompt，而是重算 612 条 rubric，核对 Judge 结构、pass 阈值、Token 和 trace，排除了默认零分与本地聚合 bug。维度上，答案的 presentation 通过率接近 48%，而事实召回和分析只有约 3%–4%。再沿 trace 和代码控制流检查，发现三个结构性瓶颈：长问题截断后四个 Worker 发出相同 query；fact extractor 不知道自己的子任务；writer 只看排序后的前 8 条事实，所以检索到的来源大量没有进入答案。

我的方案不是增加更多 Agent，而是建立一个不接触隐藏 rubric 的 coverage contract：只从用户可见问题提取 checklist，把 requirement、子任务、query、fact、citation、claim 和 verified 状态串起来；用短 query 避免截断，用跨任务轮询避免并发顺序偏置。同时把包含历史失败 attempt 的 Experiment 与零模型调用的 final snapshot 分开，既保留可靠性审计，也保证最终质量口径一致。

离线测试验证了这些行为。不过固定 Core4 真跑时，五个 Tavily Key 都被服务端拒绝。我立即停止，没有用重复付费调用掩盖问题，并补上了 SDK 异常类型识别和快速熔断。这个阶段我不会宣称质量提升；下一步是恢复凭据后重跑同一 Core4，达到质量和效率门禁后才扩大到 Core10，更不会直接跑 Full132。

## 13. 面试官可能追问

**为什么不直接把 benchmark rubric 给 Planner？** 这会泄漏评测信息，导致 benchmark 污染。coverage 只能来自用户实际可见的问题结构。

**为什么用确定性 checklist，而不是再加一个规划模型？** 当前根因是信息流断裂和 query 截断。确定性提取零额外调用、可复现、便于测试；先验证最小机制，再决定是否需要模型规划。

**把 verifier 上限从 8 提到 16 会不会更贵？** 可能会，所以它是明确的实验风险。历史 Token 中 Research 占 56.18%，目标是以更少重复检索换取更有效的 verifier/synthesis 消耗；是否成立只能由固定同题 pilot 判断。

**为什么 LangSmith 要两个 Experiment？** 原 Experiment 记录所有 attempt，适合分析运行可靠性；final snapshot 每题只回放最终结果，适合聚合最终质量。两者通过 source trace 关联，且 snapshot 不调用模型。

**Core4 全失败是否说明改造失败？** 不能。所有题在搜索阶段遭遇同一个外部认证错误，没有进入可评价的 evidence/synthesis/Judge 链路。它说明 pilot 门禁和错误分类需要加强，但不提供质量改造的正负证据。

**何时才运行 Full132？** 先恢复凭据，通过固定 Core4；再通过干净 Core10 的完成率、trace、Token、超时和本地/LangSmith 对齐门禁。只有小样本结果显示质量收益值得放大时，才承担 Full132 的时间与调用成本。
