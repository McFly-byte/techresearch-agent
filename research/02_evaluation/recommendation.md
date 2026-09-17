# 本项目评测方案建议

> 项目：面向秋招面试的开源 DeepResearchAgent
> 技术栈：Qwen 系列模型（阿里云 DashScope API）
> 目标：可复现、有基线、可消融、出图好看、失败案例可分析、API 成本可控
> 日期：2026-09-16

---

## 一、数据集组合推荐

### 核心数据集（必跑，3 个）

#### 1. MuSiQue（2-4 跳多跳问答）—— 主能力基线

- **规模**：抽 dev 集 **200 题**（按 hop 数分层：100 两跳 + 60 三跳 + 40 四跳）
- **为什么选它**：
  - 比 HotpotQA "更难作弊"，真正考多步检索推理
  - 自带问题分解（decomposition）标注，可做**过程级评测**（Agent 的分解对不对）
  - 200 题规模：跑 3 次 × 200 = 600 次 Agent 调用，成本约 ¥15-30
- **下载**：`datasets.load_dataset("bdsaglam/musique", "albert-xxlarge-v2")`

#### 2. FreshQA（时效性 + 错误前提）—— 差异化亮点

- **规模**：**全部 600 题**（数据集本身就这么大），或抽 300 题
- **为什么选它**：
  - Deep Research Agent 的卖点是"联网查最新信息"，FreshQA 直接测这个
  - 三维标注（fast-changing / false-premise / multi-hop）能出**分层报告图**
  - False-premise 子集（"Google 什么时候发布的 ChatGPT？"）考 Agent 是否敢反驳错误前提——面试加分点
- **下载**：论文附录 + NVIDIA evaluator 脚本
- **成本**：600 题 × 3 次 ≈ ¥30-50

#### 3. Bamboogle（对抗性 2 跳）—— 压力测试

- **规模**：**全部 125 题**
- **为什么选它**：
  - 125 题极小，成本几乎为零
  - 题目设计为"直接 Google 搜不到答案"，必须多步推理——这正是 Deep Research 的核心场景
  - 2025-2026 年所有 search-agent 论文都报这个数，**面试时能和 SOTA 对比**（普通模型 ~40%，好的 Agent ~70%+）
- **下载**：GitHub / HF `bamboogle` 配置

### 补充数据集（选跑，1 个）

#### 4. DeepResearch Bench（端到端报告评测）—— 面试展示核心

- **规模**：抽 **20-30 个 PhD 级任务**
- **为什么选它**：
  - 直接评"调研报告"而非短答案，最接近产品真实形态
  - 自带 4 个顶尖系统的报告 + 人工评分，可做**参照系**
  - 20-30 题足够出"我的 Agent vs 朴素 RAG vs GPT-4o"的对比表
- **下载**：`datasets.load_dataset("muset-ai/DeepResearch-Bench-Dataset")`
- **成本**：报告生成贵（每题 Agent 多轮调用 ~¥2-5），30 题 ≈ ¥100-150

### 数据集组合速查

| 数据集 | 题数 | 角色 | 成本估算 | 出什么图 |
|---|---|---|---|---|
| MuSiQue | 200 | 主能力基线 | ¥30 | 按 hop 数分层的 EM 柱状图 |
| FreshQA | 300-600 | 时效差异化 | ¥50 | 三维拆分的准确率雷达图 |
| Bamboogle | 125 | 压力测试 | ¥10 | 和 SOTA 论文的对比表 |
| DeepResearch Bench | 20-30 | 端到端报告 | ¥150 | 报告质量 LLM-judge 评分对比 |
| **合计** | **~650-950** | | **~¥250** | |

---

## 二、核心评估指标清单

### 第一层：答案正确性（端到端）

| 指标 | 含义 | 怎么算 | 为什么重要 |
|---|---|---|---|
| **EM (Exact Match)** | 答案和 ground truth 完全匹配的比例 | 字符串规范化后精确匹配 | 客观、无 judge 偏差，短答案集用 |
| **F1 (token-level)** | 答案 token 和 gold 的重叠 F1 | 对 gold 答案 token 集合 | EM 太严时的补充 |
| **Relaxed Accuracy** | 主答案是否正确（FreshQA 风格） | LLM-judge 二元判断 | 长答案/开放题用 |

### 第二层：检索质量（RAG 模块）

| 指标 | 含义 | 怎么算 |
|---|---|---|
| **Context Precision** | 检索到的文档里有多少真正相关 | RAGAS 内置 |
| **Context Recall** | gold 支撑事实里有多少被检索到 | MuSiQue/HotpotQA 的 supporting_facts 标注可算 |
| **Hits@k** | gold 文档出现在 top-k 的比例 | 简单计数 |

### 第三层：生成质量（幻觉控制）

| 指标 | 含义 | 怎么算 |
|---|---|---|
| **Faithfulness** | 回答里的论断有多少能被检索上下文支持 | RAGAS / DeepEval 内置 |
| **Citation Precision** | 引用的来源是否真的支撑所附陈述 | LLM-judge 逐条判断 |
| **Claim Coverage** | 报告里所有陈述中，被引用支撑的比例 | 拆陈述 → 查引用 |
| **Strict Accuracy**（FreshQA） | 所有陈述都事实且最新 | LLM-judge |

### 第四层：效率成本（工程维度）

| 指标 | 含义 |
|---|---|
| **平均检索轮次** | Agent 完成一题需要几次 search |
| **平均 token 消耗** | 每题 input+output tokens |
| **端到端延迟** | 从提问到出报告的秒数 |
| **成本/题** | 人民币 |

> 面试加分：别人只报准确率，你报"准确率 vs 成本"的帕累托图，立刻显出工程思维。

---

## 三、评测方案设计要点

### 3.1 数据划分（可复现）

```
MuSiQue dev (2417 题)
  ├─ 抽样 200 题 → test set（固定 random seed=42）
  └─ 不另划 dev（小项目直接用 test，README 声明）

FreshQA (600 题)
  ├─ 全量或抽 300 → test set
  └─ 抽 50 题 → human calibration set

Bamboogle (125 题)
  └─ 全量 → test set

DeepResearch Bench (100 task)
  ├─ 抽 30 题 → test set
  └─ 其中 10 题 → human calibration set
```

**README 必须写**：`random_seed = 42`，抽样脚本路径，数据版本 commit hash。

### 3.2 评估脚本结构

```
evaluation/
├── data/
│   ├── musique_sample_200.json      # 抽样好的测试集
│   ├── freshqa_sample_300.json
│   ├── bamboogle_125.json
│   └── drbench_sample_30.json
├── run_eval.py                        # 主入口：跑 Agent → 收集结果
├── metrics/
│   ├── exact_match.py                  # EM/F1
│   ├── faithfulness.py                # 调 RAGAS
│   ├── citation_precision.py          # 自定义 LLM-judge
│   └── claim_coverage.py             # 自定义拆陈述
├── judges/
│   ├── qwen_judge.py                  # Qwen-Max 当 judge
│   └── calibration.py                 # 50 条人工 vs LLM 一致性
├── reports/
│   ├── results_baseline.json          # 基线结果
│   ├── results_ablation_1.json
│   └── plots/                         # matplotlib 出图
└── README.md                           # 如何复现：pip install + API key + 命令
```

### 3.3 基线对比方案

跑 4 个系统对比：

| 系统 | 说明 | 作用 |
|---|---|---|
| **A. 朴素 RAG** | 单次检索 + 直接生成 | 最低基线 |
| **B. 你的 Agent** | 多跳规划 + 多轮检索 + 引用约束 | 主角 |
| **C. 单 Qwen-Max 无检索** | 纯参数记忆 | 证明检索的价值 |
| **D. (可选) GPT-4o 无检索** | 外部参照系 | 面试展示"开源方案 vs 闭源" |

每个系统在 4 个数据集上跑，结果画成**分组柱状图**（x=数据集，y=分数，颜色=系统）。

### 3.4 消融实验设计

```
Baseline: 朴素 RAG
  ↓ + 多跳问题分解 (decomposition)
Ablation 1: 多跳规划版
  ↓ + 每步检索结果交叉验证
Ablation 2: + 反思/验证模块
  ↓ + 强制引用约束 (每段挂引用)
Ablation 3: 完整 Agent
```

每次只加一个组件，报告每个指标的变化量。**面试时讲"我每个设计决策都有数据支撑"**，比空谈架构强 10 倍。

### 3.5 失败分析方法

1. **自动聚类**：把答错的题按错误类型分桶——
   - 检索失败（没找到正确文档）
   - 推理失败（找到了但没串联对）
   - 生成失败（推理对了但生成幻觉）
   - 时效失败（信息过时）
2. **抽 10 个典型案例**，每个写一段分析：错在哪、根因是什么、怎么修。
3. **画饼图**：错误类型分布，一眼看出主要瓶颈。

---

## 四、成本与时间估算

| 阶段 | 任务 | 预计耗时 | 预计 API 成本 |
|---|---|---|---|
| 1 | 下载数据集 + 写抽样脚本 | 半天 | ¥0 |
| 2 | 跑 4 个系统在 MuSiQue+Bamboogle（短答案） | 1 天 | ¥50 |
| 3 | 跑 FreshQA | 1 天 | ¥50 |
| 4 | 跑 DeepResearch Bench 30 题 | 2 天 | ¥150 |
| 5 | 写自定义指标脚本 | 1 天 | ¥20 |
| 6 | 人工校准 50 条 | 半天 | ¥0（自己标） |
| 7 | 出图 + 写失败分析 | 1 天 | ¥0 |
| **合计** | | **~7 天** | **~¥270** |

---

## 五、面试展示要点（加分项）

1. **一张总表**：4 系统 × 4 数据集 × 核心指标，数值清晰
2. **一张帕累托图**：x=成本/题，y=准确率，证明"我的方案性价比最高"
3. **一张消融瀑布图**：每个组件加了之后涨了多少分
4. **错误分布饼图**：证明你懂自己系统的瓶颈
5. **3 个失败案例深度剖析**：每个讲"现象→根因→改进思路"
6. **Judge 校准报告**：50 条人工 vs LLM-judge 的 Cohen's Kappa（>0.6 就敢说"我的评测可信"）

---

## 六、风险与注意事项

| 风险 | 对策 |
|---|---|
| FreshQA 答案随时间变化 | 冻结评测时间点，README 注明"评测于 2026-09-XX" |
| LLM-judge 不靠谱 | 必做 50 条人工校准，报告 Kappa |
| Qwen 当 judge 偏好 Qwen | judge 用不同 prompt 风格，或抽 20 条用 GPT-4o 交叉验证 |
| 数据泄露（训练集混入测试集） | 只用 dev/test 子集，不用 train；Bamboogle/HLE 查 contamination |
| 成本超支 | 先在 50 题小规模跑通流程，确认指标定义对了再全量跑 |
| Web 搜索结果不稳定 | 记录每次检索的 query 和返回 top-k，落盘，保证可复现 |

---

## 七、推荐的第一周执行清单

- [ ] Day 1: 下载 4 个数据集，写抽样脚本（seed=42），跑通加载
- [ ] Day 2: 写 `run_eval.py`，先在 Bamboogle 125 题上跑通朴素 RAG 基线
- [ ] Day 3: 接入 DeepEval + RAGAS，跑 faithfulness/EM
- [ ] Day 4: 跑 MuSiQue 200 题 + FreshQA 300 题的基线
- [ ] Day 5: 写 citation precision / claim coverage 自定义指标
- [ ] Day 6: 跑完整 Agent 系统 + 消融实验
- [ ] Day 7: 跑 DeepResearch Bench 30 题 + 人工校准 + 出图

---

## 主要参考

- 数据集：见 `datasets.md` 末尾链接表
- 方法论：见 `methodology.md` 末尾链接表
- 工具：见 `tools.md` 末尾链接表
- DeepResearch Bench: https://deepresearch-bench.github.io/
- ReportBench 指标设计: https://arxiv.org/html/2508.15804
- DeepScholar-Bench 指标设计: https://arxiv.org/html/2508.20033v1/
- LLM-as-judge 最佳实践: https://mer.vin/2025/11/llm-as-a-judge-best-practices-for-consistent-evaluation/
