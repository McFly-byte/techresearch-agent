# 前沿论文调研：Deep Research Agent（2025–2026）

> 调研时间：2026-09-16。聚焦 2025 年 1 月之后的工作；2023 年及更早的基础概念（ReAct、RAG 等）仅作背景，不展开。
> 所有条目均经搜索命中原文或权威二手来源，链接可点击。

---

## 0. 一句话背景（给新人）

Deep Research Agent 不是"更聪明的 RAG"，而是一个**会自己决定下一步搜什么、读什么、再搜什么**的循环式 Agent。它通常包含四个动作循环：**规划（Plan）→ 检索（Search/Browse）→ 反思（Reflect）→ 写作（Write）**，跑几十到几百步，最终产出带引用的长报告。2025 年 2 月 OpenAI 发布 Deep Research（基于 o3）后，这个方向从"实验室 demo"变成了工业界标配，论文与开源项目集中爆发。

---

## 1. 系统级论文 / 工业系统

### 1.1 OpenAI Deep Research（System Card，2025-02）
- **作者/机构**：OpenAI
- **时间**：2025 年 2 月
- **核心方法**：早期 o3 版本为网页浏览优化，**单 Agent** 架构——一个推理模型自己规划、浏览网页、读图读 PDF、写 Python 代码分析数据，根据发现的信息动态 pivot。前置有一轮简短的用户澄清（clarification）。
- **关键结论**：在 BrowseComp 上达到 51.5%，而 GPT-4o+browsing 仅 1.9%，证明"长程推理 + 浏览器工具"相比"短工具调用"有数量级差距。
- **对本项目的启发**：
  - 单 Agent + 强推理模型是一条可行路线；但开源复现不必追求单 Agent，可以用"多 Agent 协作 + 普通模型"达到类似效果，更适合面试展示。
  - **前置澄清环节**（问用户一次"你到底想要什么深度/范围"）是低成本高回报的设计，几乎所有后续系统都继承了这一步。
- **链接**：https://openai.com/index/introducing-deep-research/ （System Card 见官方页；二手分析 https://blog.bytebytego.com/p/how-openai-gemini-and-claude-use ）

### 1.2 BrowseComp: A Simple Yet Challenging Benchmark for Browsing Agents（2025-04）
- **作者/机构**：Wei et al., OpenAI
- **时间**：2025 年 4 月
- **核心方法**：构造 1,266 道需要在网上**多跳、纠缠式**找答案的问题，答案短且可机器判分。三层筛选：现有模型答不出、Google 五次搜索无首屏答案、人工 10 分钟解不出。
- **关键结论**：GPT-4o 不带浏览 0.6%、带浏览 1.9%、o1 不带浏览 9.9%、OpenAI Deep Research 51.5%。说明"能不能持续找"比"知不知道"更难。
- **对本项目的启发**：面试项目如果只在几个 demo 题上跑通，说服力很弱；应当**挑 5–10 道 BrowseComp / GAIA 题目做小样本评测**，即使分数不高，"有评测意识"本身就是亮点（绝大多数开源 demo 项目没有）。
- **链接**：https://arxiv.org/pdf/2504.12516.pdf ；中文解读 https://juejin.cn/post/7491867943309410355

---

## 2. Agentic Search / 迭代检索方向

### 2.1 Search-o1: Agentic Search-Enhanced Large Reasoning Models（2025-01，EMNLP 2025）
- **作者/机构**：LinkedIn AI 等
- **时间**：2025 年 1 月
- **核心方法**：让推理模型（LRM）在 CoT 推理过程中**自主决定何时插入检索**——在思考序列中遇到不确定点就调用搜索，拿到文档后再用一个独立的 **Reason-in-Documents 模块**对长文档做"文档内推理"提炼成精炼知识 K，再拼回思考序列继续推。
- **关键结论**：在科学、数学、代码推理任务上，把检索从"事前一次性"变成"推理中按需插入"显著提升准确性和长程一致性。
- **对本项目的启发**：
  - 你的旧项目 Paper-Agent 是"先搜完再写"，没有**检索时机决策**。新项目可以加一个 `should_retrieve(state)` 判定节点，让 Agent 自己判断"我现在知识够不够，要不要再搜一次"。
  - "Reason-in-Documents"思想：检索回来的网页太长，先做一次**结构化压缩**（提取与当前子问题相关的事实块），再进入报告写作，避免长上下文噪声。
- **链接**：https://arxiv.org/html/2501.05366 ；中文解读 https://hub.baai.ac.cn/paper/847cf199-ad06-48df-a2b4-c7a31fd90e25

### 2.2 DeepResearcher: Scaling Deep Research via RL in Real-world Environments（2025-04，EMNLP 2025）
- **作者/机构**：Yu-Xiang Zheng, Dayuan Fu 等
- **时间**：2025 年 4 月
- **核心方法**：第一个**端到端用 RL 在真实开放网页环境里训练** Deep Research Agent 的框架。多 Agent 架构：browsing agent 负责从各种网页结构里抽信息，planner agent 负责下一步。奖励直接来自任务完成度。
- **关键结论**：RL 训完后涌现出**规划、多源交叉验证、自我反思重定向、不知道就承认不知道**这些行为——这些都不是 SFT 教出来的。
- **对本项目的启发**：
  - 面试项目不必真训 RL，但可以**把这些"涌现行为"显式写成 prompt 工程**：在 planner 节点要求"列出 2 个交叉验证源再下结论"、"如果信息矛盾就显式指出"。
  - "诚实"（承认不知道）是容易被忽视的质量信号，可以做成一个"confidence score"输出到报告里。
- **链接**：https://arxiv.org/html/2504.03160v3

### 2.3 WebDancer: Towards Autonomous Information Seeking Agency（2025-05）
- **作者/机构**：Jialong Wu 等
- **时间**：2025 年 5 月
- **核心方法**：在 ReAct 基础上加入**信息 Seeking Agency 框架**——显式建模"意图—观察—下一步动作"，在 GAIA 和 WebWalkerQA 上取得强表现。
- **关键结论**：把"搜索意图"作为一等公民显式跟踪（而不是埋在 CoT 里），比裸 ReAct 显著更稳。
- **对本项目的启发**：State 里除了 messages，应维护一个显式的 `intent / goal / open_questions` 字段，每步更新；这就是 LangGraph 里 typed state 的价值。
- **链接**：https://arxiv.org/html/2505.22648v3

### 2.4 RE-Searcher: Robust Agentic Search via Goal-oriented Planning and Self-reflection（2025）
- **作者/机构**：OpenReview 论文
- **时间**：2025 年
- **核心方法**：先做系统分析证明"语义相近的 query 变体可能导致完全发散的搜索轨迹"——即**搜索路径对 query 措辞极度敏感**。解法：把**目标导向规划 + 自我反思**显式写进推理过程；训练走 SFT → GRPO，复合奖励。
- **关键结论**：显式规划+反思能显著降低搜索轨迹方差。
- **对本项目的启发**：
  - 你的迭代搜索循环需要一个**"上一轮搜得怎么样"的反思节点**：根据本轮检索结果判断 query 是太宽/太窄/跑偏，再决定下一轮 query 改写方向。
  - 这是面试讲"反思机制"最直接的论文支撑。
- **链接**：https://openreview.net/forum?id=4f4gzyD2Av

### 2.5 Search-R1（COLM 2025）
- **核心方法**：用 GRPO 风格 RL 训练 LLM"边推理边调搜索"，证明在没有人工 query 改写标注的情况下，RL 能自己学到"什么时候该搜、搜什么"。
- **对本项目的启发**：背景知识，不必复现；但说明"检索时机"是一个可学习的决策点，可在 synthesis 里作为趋势提及。
- **链接**：https://blog.csdn.net/qq_49821869/article/details/159018352

### 2.6 IterResearch: Rethinking Long-Horizon Agents via Markovian State Reconstruction（2025-11）
- **核心方法**：指出长程 Agent 上下文爆炸的根因是"历史轨迹非马尔可夫"——旧消息 relevant 与 irrelevant 混在一起。提出 **Markovian State Reconstruction**：每步只保留对下一步决策必要的状态摘要。
- **关键结论**：IterResearch-30B 在 BrowseComp-zh 45.2、GAIA 72.8、Xbench-DS 71.0，超过 OpenAI DeepResearch 的多项指标（GAIA 67.4）。
- **对本项目的启发**：
  - 这是面试项目**最容易做出亮点的点**：长循环必然上下文爆炸，做一个**状态压缩/历史裁剪模块**（保留事实证据、丢弃中间思考垃圾），就能讲出工程深度。
  - 可以在 LangGraph 里实现一个 `compress_state` 节点，定期把 worker 的原始检索结果压成"事实卡片列表"。
- **链接**：https://arxiv.org/html/2511.07327v1

---

## 3. 多 Agent 协作 / 综述类

### 3.1 STORM: Synthesis of Topic Outlines through Retrieval and Multi-perspective Question Asking（NAACL 2024，2025 持续维护）
- **作者/机构**：Stanford OVAL
- **时间**：NAACL 2024；2025 年 1 月发布 knowledge-storm v1.1（litellm 集成）
- **核心方法**：两步走——(1) **Research 阶段**：先用 LLM 生成多个"专家视角"（如历史学家、经济学家），再让 writer agent 与"模拟的专家 agent"进行多轮对话，专家基于网页检索回答 writer 的追问，逐步累积话题理解；(2) **Writing 阶段**：基于累积材料生成结构化大纲，再分节写作并附引用。
- **关键结论**：模拟多视角对话比"一次性 plan → search → write"产出的文章结构更完整、覆盖更平衡。
- **对本项目的启发**：
  - **多视角提问**是一个低成本亮点：让 planner 节点先输出 3–5 个"立场/视角"，每个视角单独跑一条检索分支，最后汇总。
  - STORM 把"研究"和"写作"显式分阶段，避免边搜边写导致的结构混乱。
- **链接**：https://github.com/stanford-oval/storm ；DeepWiki 架构解读 https://deepwiki.com/stanford-oval/storm

### 3.2 Co-STORM: Collaborative STORM（2024-09）
- **核心方法**：在 STORM 上加 **Moderator agent** 和 turn 管理策略，支持人在环（human-in-the-loop）观察 AI 专家辩论或注入提示；维护一个**动态思维导图（mind map）**作为共享概念空间。
- **关键结论**：人机协作 + 显式知识结构（mind map）比纯自动跑更可控。
- **对本项目的启发**：面试项目可以加一个**"研究大纲实时可视化"**——用 mind map 或 todo list UI 展示当前研究进度，这既是 Co-STORM 的思想，也直接解决"黑箱感"问题。
- **链接**：https://www.skillpack.co/solutions/storm-stanford

### 3.3 Deep Research Agents: A Systematic Examination and Roadmap（2025-06，综述）
- **核心方法**：系统综述，提出分类法：**静态工作流 vs 动态工作流**、**单 Agent vs 多 Agent**；对比 API 检索 vs 浏览器探索；梳理 code execution、多模态、MCP 集成。
- **对本项目的启发**：这是写 synthesis 时**引用分类框架的最佳来源**，可以直接借用"静态 vs 动态 / 单 Agent vs 多 Agent"这个 2×2。
- **链接**：https://arxiv.org/html/2506.18096v2

### 3.4 A Comprehensive Survey of Deep Research: Systems, Methodologies, and Applications（2025-06）
- **核心方法**：另一篇综述，强调混合架构（centralized reasoning + distributed gathering）的趋势；点名 Perplexity/DeepResearch、Camel-AI/OWL 为代表。
- **链接**：https://arxiv.org/html/2506.12594v1/

---

## 4. 引用验证 / 事实性方向（差异化亮点重点）

### 4.1 L-MARS: Legal Multi-Agent System with Agentic Search and Citation-Faithfulness Audit（2025-09）
- **核心方法**：法律 QA 多 Agent 系统，显式加入 **Citation-Faithfulness Audit** 节点——把报告里的每条引用抽出来，重新打开源文档验证"这句话真的在原文里吗"。
- **对本项目的启发**：这是**面试项目差异化亮点的最佳模板**：现有开源项目（gpt-researcher、STORM）几乎都不做"逐条引用回查"。你加一个 `CitationVerifier` agent：
  1. 从最终报告抽出所有 [n] 引用；
  2. 对每条引用打开原 URL，做 NLI/字符串匹配判断"支撑关系"；
  3. 不通过的引用标红或回退重写。
  - 这个模块代码量不大（约 100 行），但面试讲故事分量很重。
- **链接**：https://arxiv.org/html/2509.00761

### 4.2 Enhancing Factual Accuracy and Citation Generation via Multi-Stage Self-Verification / VeriFact-CoT（2025-09）
- **核心方法**：多阶段自验证：先生成候选答案，再让模型"模拟自己去验证"——但论文自己承认 `VerifySimulate` 阶段本身也可能幻觉（fabricate verification result）。
- **关键结论**：**纯模型自检不可靠**，必须配合外部工具（重新打开原文）。
- **对本项目的启发**：不要只靠"让 LLM 自己检查自己"，要做**工具化验证**（真的去 fetch 原文 + NLI 判定）。
- **链接**：https://arxiv.org/html/2509.05741

### 4.3 LLM Hallucinations in the Wild: Non-existent Citations（2026-05）
- **核心方法**：Cornell/UCLA/清华/UCB 联合审计 250 万篇论文的 1.11 亿条参考文献，发现 2025 年单年约 **146,932 条幻觉引用**，且系统性偏向著名男性学者。
- **对本项目的启发**：面试讲故事的"动机"弹药：引用幻觉是**真实、可量化、严重**的问题，你的 CitationVerifier 模块解决的是一个被大规模审计证实的痛点。
- **链接**：https://www.alphaxiv.org/overview/2605.07723 ；中文解读 https://hub.baai.ac.cn/paper/17a98215-b1af-445a-ba60-3d7316614dc3

---

## 5. 评测基准（做"可观测性 + 评测"亮点时引用）

| 基准 | 时间 | 规模 | 测什么 | 链接 |
|---|---|---|---|---|
| **BrowseComp** (OpenAI) | 2025-04 | 1,266 题 | 多跳纠缠式事实查找，短答案可机器判分 | https://arxiv.org/pdf/2504.12516.pdf |
| **GAIA** (Meta/HF) | 2023 底发布，2025 年成为 DR 标配 | 三级难度 | 通用助理任务，需要推理+工具 | 背景 |
| **DeepResearchGym** (Coelho et al.) | 2025-05 | 100 题，Researchy Questions 采样 | 长报告，可复现沙箱，LLM judge 打 rubric 分 | https://arxiv.org/abs/2505.19253 |
| **DeepResearch Bench** (Du et al.) | 2025 | 综合 | 引用接地的长报告评估 | （综述中被广泛引用） |
| **LiveResearchBench** (2025-10) | 2025-10 | 真实在线查询 | User-centric deep research in the wild | https://arxiv.org/html/2510.14240v1 |
| **DR³-Eval** (NJUP) | 2025 | 含用户文件、沙箱语料、多模态、检索、多文件、引用 | 比 DeepResearchGym 更接近真实 | https://nju-link.github.io/DR3-Eval/static/DR3-Eval-latest.pdf |

> **对本项目的启发**：即使不跑全量基准，也可以**手写 10–20 道 BrowseComp/GAIA 风格的小题目**，记录每道题的轨迹长度、token 消耗、最终引用准确率，做成一个 `evals/` 目录和 README 表格——这是面试区别于"跑通 demo"的关键证据。

---

## 6. 其他值得知道的系统（一句话带过）

- **WebSailor**（阿里通义，2025-07）：RL 训 web agent，"数据合成—采样轨迹—冷启动—RL"流水线。https://arxiv.org/pdf/2507.02592v1
- **Flash-Searcher**（2025-09）：DAG 并行执行多检索分支，延迟显著降低。https://arxiv.org/html/2509.25301v1
- **WebWeaver**（2025-09）：开放域 deep research agentic loop。https://arxiv.org/pdf/2509.13312v1
- **FlowSearch**（2025-10）：动态结构化知识流，planner 把 query 拆成互连子任务。https://arxiv.org/html/2510.08521v1
- **Explore Before Committing: Hypothesis-Guided Search**（2026）：假设驱动搜索，先提出假设再找证据验证，与 self-reflection 一脉相承。
- **Sage: Benchmarking and Improving Retrieval for Deep Research Agents**（2026-02）：专门针对 DR agent 的检索质量做基准。https://arxiv.org/html/2602.05975

---

## 7. 论文侧结论速览（给 synthesis 用）

1. **单 Agent 强推理 vs 多 Agent 协作**：OpenAI DR 走单 Agent（o3），但开源圈（gpt-researcher、DeerFlow、LangGraph ODR）几乎都走多 Agent supervisor-worker——对面试项目，多 Agent 是更合适的展示对象。
2. **检索时机是核心决策点**：Search-o1、RE-Searcher、DeepResearcher 都证明"何时搜、搜什么、要不要重搜"比"搜到什么"更影响最终质量。
3. **长程上下文压缩**是工程难点：IterResearch 用 Markovian 状态重建解决；DeerFlow 用 middleware 自动摘要。这是面试项目最容易切入的工程深度点。
4. **引用验证是公认短板**：L-MARS、VeriFact-CoT、幻觉引用审计三篇一起指向"必须工具化回查，不能靠 LLM 自检"。
5. **评测基准快速成熟**：2025 年从 BrowseComp（短答案）到 DeepResearchGym/DR³-Eval（长报告 rubric），已经有可复用的评测协议。
