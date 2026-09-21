# evals/ — 可复现评测

> 默认迭代集是固定的 **Core10**。全量 132 题仍需显式确认。
> Qwen judge 的结果统一标记为 `qwen-nonofficial`；
> `official_judge = not_run (requires GPT-5.5)`。

## 目录结构

```
evals/
├── adapter.py      # EvalQuestion / EvalResult / FixtureDataset
├── configs.py      # EvalConfig + 7 个消融配置（llm_only / naive_rag / full / ...）
├── metrics.py     # compute_metrics + KeywordJudge（离线占位 judge）
├── runner.py       # EvalRunner：可断点续跑、配置快照、单题失败隔离
├── subset.py       # 固定子集清单加载及源数据 hash 校验
├── subsets/        # Core10 等版本化 qid 清单
└── README.md       # 本文件
```

## 运行层级

| 层级 | 命令 | 题量 | 是否付费 |
|---|---|---|---|
| fixture | `pytest tests/unit/test_eval_harness.py` | 3 | 否 |
| Core4 pilot | `--subset core10 --limit 4` | 4 | 少量 |
| Core10 | `--subset core10` | 10 | 中 |
| 全量 132 题 | **必须显式 `--confirm-full`** | 132 | 高 |

推荐命令：

```powershell
# 先跑四题，创建唯一 Experiment；题目并发 2，每题研究 worker 1。
python -m evals.cli run `
  --dataset evals/data/deepresearch_bench_ii/tasks_and_rubrics.jsonl `
  --subset core10 --limit 4 --mode live --judge llm `
  --concurrency 2 --research-workers 1 `
  --output-dir evals/runs/drb2_core10_live_qwen_v3

# 验收后在原目录、原 Experiment 内补齐 Core10；completed 题自动跳过。
python -m evals.cli run `
  --dataset evals/data/deepresearch_bench_ii/tasks_and_rubrics.jsonl `
  --subset core10 --mode live --judge llm `
  --concurrency 2 --research-workers 1 `
  --output-dir evals/runs/drb2_core10_live_qwen_v3 --resume
```

默认时限为：整题 900 秒、研究 420 秒、验证与合成 180 秒、judge 240 秒。
默认并发为 2 道题，每题 1 个研究 worker；每题写作阶段最多并发核验 2 条声明，
因此研究和写作阶段都不会超过全局 Qwen 4 路容量。
这些值均可通过对应 CLI 参数覆盖。`--resume` 会核对数据集 hash 和子集元数据，
防止把不同数据或不同 qid 清单混入同一次实验。

## LangSmith 定位

- `experiment.json` 保存唯一 Experiment 的名称、ID、URL 和完整 Core10 qid。
- `results/<qid>.json` 保存该题的 `run_id`、`trace_id` 和 `trace_url`。
- Experiment 表格输出 qid、状态、延迟和 input/output/total token；点击该行即可进入
  该题的 root trace，并查看研究、工具、验证、合成和 judge 子节点。
- `--resume` 读取 `experiment.json` 并复用原 Experiment，不再生成带随机后缀的新实验。
- Experiment 视图会尝试隐藏 input/output/total cost，仅保留 token 指标。

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
- DeepResearch Bench II 原始数据不入库；本地数据必须通过固定 SHA-256 与 Core10
  清单绑定。版本、来源和许可证单独记录，大文件加入 `.gitignore`。

## 指标口径

- `pass_rate`：judge_score >= 0.5 的题目占比（零分母返回 0.0）
- `citation_precision`：已验证 claim 占比（阶段 4 已实现）
- `avg_search_rounds` / `avg_tokens` / `avg_latency_s`
- API 失败的题目**留在分母**里（status=failed），不静默排除

## LLM Judge

`KeywordJudge` 只用于离线结构验证。真实运行使用 rubric LLM judge，结果仍属于
非官方替代评审，不能当作官方 GPT-5.5 成绩。

## 运行门禁

1. 离线测试、Ruff、mypy 全部通过。
2. Core4 验收唯一 Experiment、qid 可定位、trace 树完整、token 非零、题目不超过
   900 秒。
3. Core4 通过后使用同一目录 `--resume` 完成 Core10。
4. Core10 稳定且迭代方案冻结后，才考虑显式启动 Full132。
