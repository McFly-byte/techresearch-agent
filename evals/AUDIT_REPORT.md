# DeepResearch Agent — evals 评测管线审计报告

> 审计日期：2026-09-18
> 审计范围：`evals/` 全部模块 + `src/` 中与评测相关的代码（prompts registry、providers、graph builder、api/runner、cli/doctor）
> 审计方式：逐文件静态阅读，未修改任何代码。

---

## 0. 结构差异说明（重要前置事实）

用户预期的文件清单（`answerer.py / benchmark.py / cli.py / data.py / judges.py / reporting.py / schemas.py`）**在项目中不存在**。实际 `evals/` 目录只有 8 个 Python 文件：

| 实际文件 | 对应预期职责 |
|---|---|
| `adapter.py` | 替代 `data.py` + `schemas.py`（含 `EvalQuestion`/`EvalResult`/`DeepResearchBench2Adapter`） |
| `configs.py` | 消融配置（`EvalConfig` + `CONFIGS`） |
| `metrics.py` | 替代 `judges.py`（`KeywordJudge`）+ `compute_metrics` |
| `runner.py` | 替代 `benchmark.py`（`EvalRunner`） |
| `baselines.py` | 替代 `answerer.py`（3 个 answerer） |
| `failures.py` | 失败分类 |
| `charts.py` | 替代 `reporting.py` |
| `__init__.py` | 空文件 |

**没有 `cli.py`，没有 `reporting.py`，没有独立 `judges.py`。** 下面按 10 个缺陷点逐项审计。

---

## 1. Keyword Judge vs LLM Rubric 评分 — **FAIL**

**证据：**
- `evals/metrics.py:49-64` — `KeywordJudge` 是唯一的 judge：`score(answer, required_keywords)` 只做小写子串匹配，命中数 / 关键词总数。
- `evals/runner.py:39` — `self._judge = KeywordJudge()`，硬编码，无任何注入/替换路径。
- `evals/runner.py:83` — `score, reason = self._judge.score(answer, q.required_keywords)`，**只传 `required_keywords`，从不传 `q.rubrics`**。
- `evals/README.md:54-58` 自述："当前是 `KeywordJudge`（离线关键词匹配），**不是** LLM judge。"
- `src/core/prompts/manifest.json` 只含 4 个 prompt：`fact_extraction_system`、`fact_extraction_repair`、`citation_verifier_system`、`citation_verifier_user`。**没有任何 eval judge / rubric 评分 prompt。**
- `src/core/prompts/manifest.lock.json` 只 pin 了上述 4 个 prompt 的 commit（`9bdf4de1`、`52edcb16`、`7e796f5b`、`17576e9c`），**没有 eval judge prompt 被 push 到 LangSmith Registry**。

**问题：**
1. 官方 DeepResearch Bench II 的评分必须基于 `rubric_items`（info_recall / analysis / presentation 三维度逐条打分），而当前 judge 完全不读 `rubrics` 字段。
2. `DeepResearchBench2Adapter`（`adapter.py:126`）把 `required_keywords=[]` 设为空列表。`KeywordJudge.score()` 在空列表时直接返回 `(1.0, "no required keywords")`（`metrics.py:59-60`）。**结论：对 DRB2 全量数据，每道题 judge_score 恒等于 1.0，指标完全无意义。**
3. 无 LLM rubric judge 路径、无 judge prompt 在 LangSmith 管理、无 commit 固定。

**修复建议：**
- 实现 `LLMRubricJudge`：输入 `(answer, rubrics: list[str])`，逐条 rubric 用 LLM 打分 0/1，取均值。
- 将 judge prompt 纳入 `manifest.json` + `manifest.lock.json`，通过 `PromptRegistry` 拉取固定 commit。
- `EvalRunner` 应接受可注入的 `judge` 参数（当前硬编码），并把 `q.rubrics` 传给 judge。

---

## 2. Answerer 答案泄漏 — **FAIL**

**证据：**
- `evals/runner.py:77` — 离线默认 answerer（`answerer is None` 分支）：
  ```python
  answer = f"Answer: {q.question} — see {q.reference_answer}"
  ```
  直接把 `reference_answer` 拼进答案文本。然后 `runner.py:83` 用这个含标准答案的 answer 去喂 `KeywordJudge`。**标准答案既给了 research agent（通过 answer 文本），又给了 judge，等于自答自评。**
- `evals/adapter.py:24-25` — `EvalQuestion` 同时携带 `reference_answer` 和 `rubrics`。
- `evals/baselines.py:28-56` — 三个真实 baseline answerer 只用 `q.question`，不读 `q.reference_answer`/`q.rubrics`（这一点是对的）。
- 但 `runner.py:82` 把整个 `q: EvalQuestion` 对象传给 answerer，没有任何字段隔离机制——answerer 可以自由访问 `reference_answer`。

**问题：**
1. 离线 fixture 路径存在确定性答案泄漏（runner.py:77）。
2. 设计上没有强制隔离：研究 agent 拿到的是完整 `EvalQuestion`，而非只含 `question` 的子集。

**修复建议：**
- 删除 runner.py:77 中拼接 `reference_answer` 的逻辑；离线 answerer 应返回与研究 agent 相同形态的输出。
- 把传给 answerer 的对象收窄为只含 `qid` + `question`（可定义 `EvalPrompt` dataclass），`reference_answer`/`rubrics` 只在 judge 阶段从 `EvalQuestion` 取。

---

## 3. 同步调用 async — **FAIL**

**证据：**
- `evals/runner.py:61` — `def run(self, ...)` 是**同步**函数。
- `evals/runner.py:82` — `answer, rounds, tokens = self._answerer(q)` 同步解包调用。
- `evals/baselines.py:28` — `async def answer_llm_only(q)`
- `evals/baselines.py:33` — `async def answer_naive_rag(q)`（内部 `await search.search(...)`、`await fetcher.fetch(...)`）
- `evals/baselines.py:45` — `async def answer_full(q)`（内部 `await graph.ainvoke(...)`）
- 全部三个 `BASELINES` 值都是 async 协程函数。

**问题：**
把 `BASELINES["full"]` 传入 `EvalRunner` 时，`self._answerer(q)` 返回一个 **coroutine 对象**，不会执行；`answer, rounds, tokens = <coroutine>` 抛 `TypeError: cannot unpack non-iterable coroutine object`。该异常被 `runner.py:95` 的 `except Exception` 捕获，**每道题都标记为 failed**，且协程从未 await（Python 会发 `RuntimeWarning: coroutine was never awaited`）。

测试未暴露此 bug：`tests/unit/test_eval_harness.py:49,76` 用的是 **同步** `def answerer(q)`，不触发。`tests/unit/test_phase5_corrections.py:49` 只断言 `set(BASELINES)` 名字，从未实际调用。

`docs/implementation-progress.md:39` 已自行记录此缺陷。

**修复建议：**
- `EvalRunner.run()` 改为 `async def run()`，内部 `await self._answerer(q)`。
- 或在 runner 内检测协程并用 `asyncio.run()` 包一层（不推荐，嵌套事件循环有坑）。推荐前者，runner 全异步化。

---

## 4. 消融实验不生效 — **FAIL**

**证据：**
- `evals/configs.py:19-23` — `EvalConfig` 定义了 `use_retrieval / use_reflection / use_verifier / use_supervisor / use_long_term_memory` 五个开关。
- `evals/runner.py` — **从不读取任何 `config.use_*` 字段**。runner 只把 `config` 写入 snapshot（`runner.py:54-59`），不据此改变行为。
- `evals/runner.py:82` — answerer 只收到 `q`，**收不到 config**。answerer 无法知道开关状态。
- `evals/baselines.py:59-63` — `BASELINES` 只有 3 个：`llm_only / naive_rag / full`。`no_verifier / no_reflection / single_agent / no_long_term_memory` 四个消融配置**没有对应的 answerer 实现**。
- `evals/baselines.py:45-56` — `answer_full` 硬编码 `build_graph(worker=worker, budget=budget)`，无视 config。
- `src/graph/builder.py:58-67` — `build_graph()` 不接受 `use_verifier / use_reflection / use_supervisor` 参数，图结构是固定的。

**问题：**
1. 7 个消融配置中只有 3 个（llm_only / naive_rag / full）有对应实现，其余 4 个只是标签。
2. 即使是有实现的 3 个，config 开关也不影响行为——`full` 永远跑同一个 graph。
3. 消融结论无法成立：不同 config 跑出的曲线差异只来自手动选了不同 baseline 函数，而非配置驱动。

**修复建议：**
- `build_graph()` 增加 `use_verifier / use_reflection / use_supervisor` 参数，真正短路对应节点。
- `EvalRunner` 把 `config` 传给 answerer，由一个统一的 `build_answerer(config)` 工厂根据开关装配不同子系统。
- 为 `no_verifier / no_reflection / single_agent` 补实现。

---

## 5. CLI 完整性 — **FAIL**

**证据：**
- `evals/` 目录下**没有 `cli.py`、没有 `__main__.py`**（目录列表已确认）。
- `pyproject.toml:48-51` — 唯一的 console_scripts 入口是 `tra = "cli.doctor:main"`。
- `src/cli/doctor.py` — 只有 `doctor / config / research / run / prompts` 子命令，**没有任何 `evals run / evals list / evals report` 子命令**。
- `evals/README.md:24` 和 `evals/runner.py:9` 都提到 `--confirm-full` 标志，但**代码中不存在该标志**（grep 全项目零命中实现）。

**缺失的 CLI 参数：**
| 参数 | 状态 |
|---|---|
| model | 缺失 |
| provider | 缺失 |
| prompt commit | 缺失 |
| dataset path / hash | 缺失 |
| 并发 (max_workers) | runner 内部固定串行，CLI 无法配 |
| 预算 (max_cost_usd) | config 有字段但 runner 不执行 |
| 断点恢复 | runner 支持（见 #6）但无 CLI 暴露 |
| `--confirm-full` | 仅文档提及，未实现 |
| `run / list / report` 子命令 | 全缺失 |

**修复建议：**
- 新建 `evals/cli.py`，注册 `tra evals run/list/report` 子命令。
- 暴露 `--model --provider --dataset --prompt-commit --max-workers --max-cost --confirm-full --resume`。

---

## 6. 断点恢复 — **PARTIAL**

**证据：**
- `evals/runner.py:42-52` — `_load_done()` 加载 `results/*.json`，已存在 qid 的题跳过（`runner.py:70-72`），不重复调用 answerer。✓ 已完成题不重复收费。
- **但** `runner.py:47` 的 glob 加载**所有** `.json`，包括 `status="failed"` 的题。`runner.py:70` `if q.qid in done: continue` 把 failed 题也当作 done 跳过。**失败题不会被重试。**
- 无任何重试逻辑（grep `retry` 在 evals/ 下零命中；src/ 中的 retry 都在 research worker 内部，不在评测层）。
- `evals/runner.py:106-108` — 每道题写完 `results/<qid>.json`，但文件名不含 config 名。**两个消融配置跑同一 out_dir 会互相覆盖结果文件和 summary.json。**

**问题：**
1. 已完成题跳过 ✓（好）。
2. 失败题**不重试**，且 resume 后永久跳过（因为它的 .json 已落盘）。
3. 无 per-config 目录隔离，多消融并行跑会数据污染。

**修复建议：**
- `_load_done` 只跳过 `status=="completed"` 的题；`failed` 题重新执行。
- 支持 `--retry-failed` 标志。
- 结果路径改为 `results/<config_name>/<qid>.json`，summary 按 config 分文件。

---

## 7. 元数据记录 — **FAIL**

**证据：**
- `evals/configs.py:35-51` — `snapshot()` 记录：
  - `git_commit` ✓（subprocess 取 `git rev-parse HEAD`）
  - `python / platform` ✓
  - `prompt_version: "eval-v1"` — **硬编码字符串**，不是真实 LangSmith commit。
  - `code_version: "0.0.0"` — **硬编码**。
  - `model_label: "fake-heuristic"` — **硬编码默认值**（`configs.py:33`），真实运行时不会自动改成实际模型。
- **缺失：**
  - 实际 provider 名称（qwen / fake）— 未记录
  - 实际 model id — 未记录
  - LangSmith prompt commit — 未记录（prompt_version 是假的）
  - dataset 文件 hash — 未记录（grep `sha256` 在 evals/ 零命中）
  - dataset 版本 — `DeepResearchBench2Adapter.version="v0.2"` 是代码里写死的，不是数据文件的实际版本
  - usage（token 真实消耗）— `tokens_estimated` 是估算值，baselines 里是硬编码（`answer_full` 用 `budget.tokens_used()`，但 fake 模式无真实计费）
  - latency — 每题记录了 `latency_s` ✓，summary 聚合了 `avg_latency_s` ✓

**修复建议：**
- snapshot 增加：`provider`、`model_id`、`prompt_commits`（从 `manifest.lock.json` 读）、`dataset_sha256`、`dataset_version`、`usage`。
- `model_label` 改为运行时从实际 provider 取，而非 dataclass 默认值。

---

## 8. 失败处理 — **PARTIAL**

**证据：**
- `evals/runner.py:95-104` — 单题异常捕获，标记 `status="failed"`，记录 `error=str(e)`。✓
- `evals/runner.py:98-103` — failed 题保留 `error` 字符串，并调用 `classify_failure()` 分桶。✓
- `evals/metrics.py:32-34` — `n_failed = n - n_done`，failed 题**留在分母**（`compute_metrics` 用总 `n`）。✓ 不静默排除。
- `evals/failures.py:18-33` — 五桶分类（retrieval / reasoning / hallucination / freshness / tool）。
- **但** `runner.py:99-103` — failed 题的 `answer` 字段为空字符串（`EvalResult` 默认 `answer=""`），**没有保留原始安全输出 / 部分输出**。如果 research agent 跑了一半出结果但最后一步报错，中间产物丢失。
- `runner.py:101-103` — `classify_failure` 的入参是 `EvalResult(qid=q.qid, status="failed", error=str(e))`，但 `classify_failure` 只看 `result.error` 字符串匹配关键词（`failures.py:22-33`），**纯字符串启发式**，准确率低。

**修复建议：**
- failed 时保留已产出的部分 answer / facts / citations 到结果 JSON。
- 失败分类可接受启发式但需人工复核通道（README 待办 #6 已提及）。

---

## 9. 数据加载 — **PARTIAL**

**证据：**
- `evals/adapter.py:85-131` — `DeepResearchBench2Adapter` 逐行 `json.loads`，读 `task` 和 `rubric_items`。
- `adapter.py:114-120` — 用 `.get("task", "")`、`.get("rubric_items", {})`，**无必填字段校验**。如果某行缺 `task`，会得到 `question=""` 但不报错。
- `adapter.py:121-129` — `reference_answer=""`、`required_keywords=[]` 硬编码空。
- `adapter.py:123` — `qid=f"drb2-{i:04d}"`，**用行号生成 qid**。数据文件增删行或排序变化会导致 qid 漂移，破坏断点恢复（#6）。
- **无 dataset hash / 版本锁定**（grep 确认 evals/ 无 sha256）。
- **无 schema 校验**（无 pydantic model，无 `rubric_items` 结构校验——`info_recall/analysis/presentation` 维度名未校验）。
- `adapter.py:88-93` 注释里的 JSON 结构是作者**猜测**的；`README.md:45` 自述"当前未联网核对官方仓库的最新格式"。

**问题：**
1. 能加载 JSONL 基本结构，但 schema 校验不完整。
2. rubrics 被加载后**从未被 judge 使用**（见 #1）。
3. qid 不稳定。
4. 未核对官方格式，存在解析错位风险。

**修复建议：**
- 用 pydantic model 校验每行 JSON（task 必填非空、rubric_items 维度校验）。
- qid 改为数据文件自带 ID 或文件内容的 hash，而非行号。
- 计算 dataset 文件 sha256 并写入 snapshot。
- 联网核对官方仓库当前格式后再固化 adapter。

---

## 10. Research Runner 集成 — **FAIL**

**证据：**
- `evals/baselines.py:45-56` — `answer_full` 直接 `from graph.builder import build_graph` + `WorkerNode` + `FakeSearchProvider`/`FakeFetcher`，**绕过了 `src/api/runner.py` 的 `ResearchRunner`**。
- `src/api/runner.py:191-388` — `ResearchRunner` 有完整的 fake/live 模式切换（`_fake_kit` vs `_live_kit`，`runner.py:94-150`）、预算构建（`_build_budget`）、模型路由（`_build_router`）、LLM NLI verifier（`runner.py:330-337`）、SSE 事件。
- **但 evals baselines 完全不用 `ResearchRunner`**：
  - `answer_full` 硬编码 `FakeSearchProvider`/`FakeFetcher`（`baselines.py:47-48`），**永远 fake**，无 live 切换。
  - 不接 `WorkerNode` 的真实 LLM provider（`baselines.py:50` 的 `WorkerNode` 没传 `llm_provider` 参数，走默认）。
  - 不跑 verifier（`baselines.py:55` 只取 `report_markdown`，不调 `VerifiedReportBuilder`）。
- `src/api/runner.py:237-281` — `ResearchRunner.run()` 有 60 秒超时、空结果守卫、NLI 验证，这些在 evals 路径里全部缺失。

**问题：**
1. evals 跑的研究系统 ≠ 线上服务用的研究系统（两套装配）。评测结果不代表线上行为。
2. 无 live 模式——baselines 永远用 FakeSearchProvider/FakeFetcher，无法接真实 Tavily/Qwen。
3. 无 verifier、无预算、无超时。

**修复建议：**
- evals answerer 应通过 `ResearchRunner`（注入 mode="fake"/"live"）来跑研究系统，而非自己 `build_graph`。
- 统一 fake/live kit 构造，复用 `api/runner.py` 的 `_fake_kit`/`_live_kit`。

---

## 附加发现（不在 10 点内但影响运行）

| # | 位置 | 问题 |
|---|---|---|
| A1 | `evals/metrics.py:21-25` | `pass_rate` 属性 `return sum(1 for _ in []) / self.n` — **恒等于 0**，是死代码占位符。`compute_metrics` 和 `summary.json` 都不用它。 |
| A2 | `evals/runner.py:62` | `_write_snapshot()` 每次 run 都覆盖 `config_snapshot.json`，多 config 跑同目录会丢历史 snapshot。 |
| A3 | `evals/runner.py:67` | 纯串行 `for` 循环，`config.max_workers=2` 不生效（无 asyncio.gather / ThreadPool）。132 题全量将极慢。 |
| A4 | `evals/configs.py:31` | `max_cost_usd=1.0` 定义了但 runner 从不读取执行，无预算熔断。 |
| A5 | `evals/charts.py:30` | SVG 文本未转义（`<text>{label}</text>`），qid 含特殊字符可 XSS——README 已自记。 |
| A6 | `evals/baselines.py:28-30` | `answer_llm_only` 不调任何 LLM，直接返回字符串+硬编码 token（docs 已自记）。 |

---

## 总体评估

**当前管线不能直接运行官方 DeepResearch Bench II 全量评测。** 即使装上 Key、下载了数据集，跑出来的数字也没有任何意义。

### 必须修复的优先级清单

**P0 — 阻塞全量评测（不修则结果无效）：**
1. **实现 LLM rubric judge**（#1）：当前 judge 对 DRB2 数据恒输出 1.0。这是最致命的问题——跑完全量也是 100% pass。
2. **修复 async/sync 调用**（#3）：接入真实 baseline 后每道题必 failed。
3. **接入 ResearchRunner live 模式**（#10）：当前 evals 永远 fake，不调真实 LLM/搜索。
4. **实现 CLI + `--confirm-full` + 预算熔断**（#5, A4）：没有 CLI 根本无法启动全量；无预算熔断会失控。

**P1 — 影响结果可信度：**
5. **消融配置真正接线**（#4）：当前 4/7 消融无实现，配置不改变行为。
6. **答案泄漏隔离**（#2）：离线路径泄漏 reference_answer。
7. **元数据补全**（#7）：缺 provider/model/prompt commit/dataset hash，结果不可复现。
8. **失败题重试 + per-config 目录隔离**（#6）。

**P2 — 工程质量：**
9. **数据加载 schema 校验 + qid 稳定化 + dataset hash**（#9）。
10. **并发执行**（A3）：132 题串行不可接受。
11. **修复 pass_rate 死代码**（A1）、snapshot 覆盖（A2）、XSS（A5）。
12. **联网核对官方 DRB2 格式**（README 待办 #1）。

> 项目自身 `evals/README.md` 和 `docs/implementation-progress.md:31-46` 已坦承大部分上述缺陷，状态为"仅通过 fixture/smoke 验证，未运行任何付费模型正式评测"。本审计确认这些自述属实，且补充了 README 未明确指出的新问题（pass_rate 死代码、qid 漂移、per-config 覆盖、失败题不重试）。
