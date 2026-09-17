# evals/ — 可复现评测

> **当前状态：仅通过 fixture / smoke 验证。未运行任何付费模型的正式评测。**
> 不要把这里的任何数字当成项目成绩。

## 目录结构

```
evals/
├── adapter.py      # EvalQuestion / EvalResult / FixtureDataset
├── configs.py      # EvalConfig + 7 个消融配置（llm_only / naive_rag / full / ...）
├── metrics.py     # compute_metrics + KeywordJudge（离线占位 judge）
├── runner.py       # EvalRunner：可断点续跑、配置快照、单题失败隔离
└── README.md       # 本文件
```

## 运行层级

| 层级 | 命令 | 题量 | 是否付费 |
|---|---|---|---|
| fixture | `pytest tests/unit/test_eval_harness.py` | 3 | 否 |
| smoke   | 手动跑 2-3 题（需 Key） | 2-3 | 少量 |
| 正式子集 | 手动，20-30 题 | 20-30 | 中 |
| 全量 132 题 | **必须显式 `--confirm-full`** | 132 | 高 |

全量运行默认关闭。需要 Key 和预算确认后才允许启动。

## 消融配置

所有配置共享 seed / max_workers / max_iterations，只关闭一个组件：

- `llm_only`：无检索、无反思、无验证、单 agent
- `naive_rag`：单轮检索 + 直接生成，无反思/验证
- `full`：完整系统
- `no_verifier`：关 CitationVerifier
- `no_reflection`：关 Worker 循环反思
- `single_agent`：关 Supervisor-Worker
- `no_long_term_memory`：关长期记忆（MVP 默认就关）

## 数据许可

- `FixtureDataset` 是合成数据，仅用于测试，无外部许可问题。
- DeepResearch Bench II 等官方数据集**不入库**。需要时通过下载脚本获取，
  并记录版本/commit/hash/许可证。大文件加入 `.gitignore`。
- 当前未联网核对官方仓库的最新格式，需用户手动确认后再写 adapter。

## 指标口径

- `pass_rate`：judge_score >= 0.5 的题目占比（零分母返回 0.0）
- `citation_precision`：已验证 claim 占比（阶段 4 已实现）
- `avg_search_rounds` / `avg_tokens` / `avg_latency_s`
- API 失败的题目**留在分母**里（status=failed），不静默排除

## LLM Judge

当前是 `KeywordJudge`（离线关键词匹配），**不是** LLM judge。
真实 LLM judge 的 prompt 和版本需纳入配置；judge 输入/输出落盘后才能宣称与
人类一致。目前**没有**这种证据。

## 待办（需要真实 Key 才能做）

1. 写 DeepResearch Bench II adapter（需先核对官方仓库格式）
2. 跑 2-3 题 smoke，确认管线
3. 跑 20-30 题正式子集
4. 接真实 LLM judge，做少量人工校准
5. 生成分组柱状图 / 帕累托图（从 summary.json 程序化生成）
6. 失败分类与人工复核最多 10 个代表性案例
