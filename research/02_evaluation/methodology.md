# 评估方法论笔记

> 调研时间：2026-09-16
> 目标：为 Agent 开发新人讲清楚"怎么评 Deep Research Agent"——每个指标是什么、为什么重要、怎么落地。

---

## 一、LLM-as-judge 最佳实践

### 1.1 为什么用 LLM 当裁判

Deep Research Agent 的输出是**长篇调研报告**，没有标准答案，传统的 BLEU/ROUGE 对长文本几乎无效（同义改写就丢分）。LLM-as-judge 让一个强模型按 rubric 给分，是 2025-2026 年长文本评测的事实标准。

### 1.2 选哪个模型当 judge

| 档位 | 模型 | 适用场景 | 成本 |
|---|---|---|---|
| 前沿旗舰 | GPT-4o / Claude / Gemini Pro | **校准用**（跑 50-100 条人工对比，确定 judge 与人类一致性） | 高 |
| 蒸馏小 judge | Qwen-Max / gpt-4o-mini / 专用 judge 模型（JudgeLM-13B 等） | **批量跑评测** | 低 10-50x |
| 自建微调 judge | 在自己的 gold-set 上微调 | 生产环境最高准确率 | 中（一次性） |

**本项目建议**：用 Qwen-Max（DashScope）做批量 judge（因为项目本身就用 Qwen，同一 API 省钱省事），再用 GPT-4o 或 Claude 抽 50 条做校准对比。

### 1.3 减少偏差的 6 个技巧

1. **Position Bias（位置偏差）**：模型倾向选先出现/后出现的答案。对策：**Position Swap**——A/B 两份回答交换顺序各评一次，取平均。成本翻倍但消除偏差。
2. **Verbosity Bias（冗长偏差）**：模型偏好更长的回答。对策：rubric 里明确"长度不是优点"，或用二元评分（pass/fail）而非 1-5 分。
3. **Self-Enhancement Bias（自我偏好）**：Qwen 当 judge 会偏好 Qwen 生成的回答。对策：**judge 模型不要和被测模型同家族**；或用 position swap + 多 judge 投票。
4. **CoT 思维链**：让 judge 先写理由再打分，准确率 +2-5%，token +30%。值得开。
5. **Few-shot Exemplars**：在 prompt 里给 1-2 个"满分样例"和"零分样例"，比纯文字定义准得多。
6. **收紧评分量表**：避免 1-10 分，用**二元（0/1）或 3 点量表（0=错/1=部分/2=对）**。研究表明人和模型都分不清"3 分和 4 分"。

### 1.4 校准流程（必须做）

```
Step 1: 从测试集抽 50 条，人工按 rubric 打分
Step 2: 用 LLM-judge 打同样 50 条
Step 3: 计算 LLM-judge 与人工的一致率
        - 一致率 > 80% → judge 可用
        - 一致率 70-80% → 调 rubric / prompt 后重测
        - 一致率 < 70% → 换 judge 模型或换评分方式
Step 4: 记录 Kappa（见人工评估章节），写进论文/README
```

**关键原则**：Judge 的可信性来自"和人类判断一致"，不做校准的 LLM-judge 分数不可信。

### 1.5 参考来源

- LLM-as-judge 鲁棒性研究：https://arxiv.org/pdf/2506.09443v1.pdf
- Judge 偏差修复：https://arxiv.org/html/2504.09946v1
- 最佳实践博客：https://mer.vin/2025/11/llm-as-a-judge-best-practices-for-consistent-evaluation/
- Adaline 可靠性分析：https://www.adaline.ai/blog/llm-as-a-judge-reliability-bias

---

## 二、引用质量评估（Citation / Attribution）

Deep Research Agent 的核心承诺是"每个论断都有来源"，引用质量是区分"真调研"和"长篇幻觉"的关键。

### 2.1 核心指标定义

| 指标 | 定义 | 为什么重要 |
|---|---|---|
| **Citation Precision（引用精度）** | 被引用的来源中，真正支撑所附陈述的比例 | 防止"挂羊头卖狗肉"——引用了但来源不支持该论断 |
| **Citation Recall / Reference Coverage（引用召回）** | gold-standard 关键文献中被 Agent 检索到的比例 | 防止漏检关键文献，考"调研的全面性" |
| **Claim Coverage（陈述覆盖率）** | Agent 报告中所有陈述里，被引用来源完整支撑的比例 | 防止"未引用的裸论断"——这是幻觉的重灾区 |
| **Nugget Coverage（信息块覆盖）** | 从人写综述中自动抽取的"关键事实块"中，Agent 报告覆盖了多少 | 考内容覆盖度，比字面 overlap 更语义化 |
| **Any Citation Rate（有引用率）** | 回答中带引用的比例 | 基础溯源能力指标 |

### 2.2 计算方法（可复现）

**Citation Precision 的自动化计算**：
```
对报告中每个 (claim, cited_source) 对：
  1. 用 NLI 模型（如 HHEMv2）或 LLM-judge 判断：
     "cited_source 能否 entail claim?"
  2. 是 → supported；否 → unsupported
Citation Precision = supported / total_cited_claims
```

**Claim Coverage**：
```
1. 用 LLM 把 Agent 报告拆成原子陈述列表 C = [c1, c2, ..., cn]
2. 对每个 ci，检查它是否附带引用，且引用来源支撑 ci
3. Claim Coverage = 被支撑的陈述数 / 总陈述数
```

**Reference Coverage**（需要 gold reference 集）：
```
Reference Recall = |Agent 引用集合 ∩ Gold 引用集合| / |Gold 引用集合|
Reference Precision = |Agent 引用集合 ∩ Gold 引用集合| / |Agent 引用集合|
F1 = 2*P*R/(P+R)
```

### 2.3 2025 年前沿方法

- **CiteGuard**（arXiv:2510.17853）：用 RAG 验证引用归属，报告 zero-shot abstract 引用 precision=1.0 但 recall 仅 0.17——说明模型"引用了就靠谱"但"漏引很多"。
- **CiteEval**（arXiv:2506.01829）：原则驱动的引用评测，区分 "Cited" 和 "Full" 两种场景。
- **"Cited but Not Verified"**（arXiv:2605.06635）：2026 年研究发现 Anthropic 模型引用量少但事实准（77% Fact Check），Gemini 居中——**引用数量和质量存在 trade-off**，评测时不要只数引用条数。

### 2.4 本项目落地建议

- 必做：**Claim Coverage**（自动化，拆陈述→查引用支撑）
- 必做：**Citation Precision**（LLM-judge，抽 20% 人工校验）
- 选做：**Reference Coverage**（需要 gold reference 集，用 SciReviewGen 或 ReportBench 的 topic）

---

## 三、事实一致性 / 幻觉检测（Faithfulness）

### 3.1 什么是 Faithfulness

**Faithfulness（忠实度）= 生成回答中的每一句话，都能从检索到的上下文里推出来。** 它和"答案对不对"是两回事：上下文本身错了，回答忠实于上下文，faithfulness 仍然高，但答案是错的。

### 3.2 三种主流方法

| 方法 | 原理 | 优点 | 缺点 |
|---|---|---|---|
| **NLI 蕴涵**（如 HHEMv2） | 把回答拆成 claim，用 NLI 模型判断上下文是否 entail 该 claim | 快速、确定性、可批量 | 依赖 NLI 模型质量 |
| **RAGAS Faithfulness** | LLM-judge：给回答 + 上下文，问"每个 claim 是否被上下文支持" | 语义判断强 | 贵、有 judge 偏差 |
| **QA-based** | 从回答里生成子问题，看能否从上下文答出 | 无 LLM-judge 也能做 | 实现复杂 |

### 3.3 本项目推荐

用 **RAGAS Faithfulness** 作为主指标（行业标准，论文多），底层 judge 用 Qwen-Max。公式：

```
Faithfulness = 被上下文支持的 claim 数 / 总 claim 数
```

预期基线：一个朴素 RAG 系统 faithfulness 通常在 0.6-0.8；加引用约束（强制每段挂引用）后能到 0.85+。

---

## 四、人工评估协议设计

### 4.1 什么时候必须做人工评估

- LLM-judge 校准时（50-100 条）
- 面试展示的"亮点案例"（挑 5-10 个成败案例详评）
- 最终结论需要可解释性时

### 4.2 标注协议

1. **标注员**：至少 2 人独立标注；分歧时第 3 人仲裁（adjudication）。
2. **标注指南**：写清楚每个维度的 0/1/2 分定义，给 2 个正例 + 2 个反例。
3. **随机打乱**：匿名化模型来源，随机化顺序，防止偏见。
4. **双阶段**：先独立标，再讨论分歧，最后仲裁定稿。

### 4.3 一致性指标（IAA）

| 指标 | 适用 | 合格线 |
|---|---|---|
| Cohen's Kappa（2 人） | 2 个标注员 | > 0.6 勉强可用，> 0.75 优秀 |
| Fleiss' Kappa（多人） | >2 个标注员看同一批 | 同上 |
| Krippendorff's Alpha | 不完全重叠的标注 | > 0.67 |
| % Agreement（一致率） | 快速参考 | > 80% |

**关键**：Kappa < 0.4 的数据是噪声，不能作为 gold label。2026 年论文（arXiv:2606.07936）发现：**二元问题（是/否）一致性最高**（81%, κ=0.51），多选题次之——所以人工评估尽量用二元判断。

### 4.4 本项目落地方案

- 不搞大规模众包（没预算），自己 + 1 个朋友标 50 条，算 Cohen's Kappa。
- 维度：① 答案正确性 ② 引用支撑度 ③ 内容全面性 ④ 结构清晰度，各 0/1/2 分。
- 在 README 里报告 Kappa 值，面试时这就是"严谨性"的证明。

---

## 五、端到端 vs 分模块评估

### 5.1 取舍

| 维度 | 端到端评估（E2E） | 分模块评估 |
|---|---|---|
| 测什么 | 最终调研报告质量 | 检索模块、推理模块、生成模块各自的表现 |
| 优点 | 贴近用户真实体验 | 能定位"瓶颈在哪" |
| 缺点 | 黑盒，不知道哪错了 | 需要知道内部结构，实现复杂 |
| 面试价值 | 出"总分对比图"好看 | 出"消融实验"有深度 |

### 5.2 本项目建议：两者都做

**第一层：端到端（出总分图）**
- 在 HotpotQA/Bamboogle 上跑 EM（短答案）
- 在 DeepResearch Bench / 自建 100 题上跑 LLM-judge 综合分

**第二层：分模块（出消融图）**
- **Retriever 模块**：Context Precision / Recall（检索到的文档里有多少是相关的）
- **Planner/Reasoner 模块**：多跳分解正确率（MuSiQue decomposition 标注可对照）
- **Generator 模块**：Faithfulness / Citation Precision

**消融实验设计**：
```
Baseline: 朴素 RAG（无多跳规划、无引用约束）
Ablation 1: + 多跳分解规划
Ablation 2: + 引用强制约束
Ablation 3: + 反思/验证模块
每次只加一个组件，看总分变化 → 证明每个设计决策都有贡献
```

---

## 六、统计显著性与样本量

### 6.1 为什么几百条够了

- 面试项目不是论文，不需要 p<0.001；**100-300 条 + 每次跑 3 次取平均**就足够展示趋势。
- 报告**均值 ± 标准差**（跑 3 次），比单次结果可信。
- 对比时看**绝对差值 > 5%** 才有意义（<5% 可能是噪声）。

### 6.2 成本控制（Qwen/DashScope）

假设用 Qwen-Max 做 judge：
- 每条样本 judge 调用 ~2K input + 500 output tokens
- 300 条 × 3 次 × 2.5K tokens ≈ 2.25M tokens
- 加上被测 Agent 本身的多轮检索调用，总预算可控在 **几十元人民币** 量级。

---

## 主要来源链接

- LLM-as-judge 鲁棒性：https://arxiv.org/pdf/2506.09443v1.pdf
- Judge 偏差策略：https://arxiv.org/html/2504.09946v1
- 最佳实践：https://mer.vin/2025/11/llm-as-a-judge-best-practices-for-consistent-evaluation/
- Judge 可靠性：https://www.adaline.ai/blog/llm-as-a-judge-reliability-bias
- 引用归属评测：https://arxiv.org/html/2605.06635
- CiteGuard：https://arxiv.org/html/2510.17853v3/
- CiteEval：https://arxiv.org/html/2506.01829v1
- ReportBench 指标：https://arxiv.org/html/2508.15804
- DeepScholar-Bench 指标：https://arxiv.org/html/2508.20033v1/
- 人工评估协议分析：https://arxiv.org/html/2606.07936v1
- IAA 实践指南：https://datavlab.ai/post/inter-annotator-agreement-llm-evaluation-guide
- RAGAS 文档：https://docs.ragas.io/en/v0.2.11/howtos/integrations/langchain/
