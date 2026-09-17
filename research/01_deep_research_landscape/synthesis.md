# 综合分析：2026 年 Deep Research Agent 技术全景与面试项目设计建议

> 写作时间：2026-09-16。基于同目录下 `papers.md`、`open_source_projects.md`、`blogs.md` 三份调研笔记。
> 目标读者：Agent 开发新人，正在做一个面向秋招面试的开源 DeepResearchAgent 项目（旧项目 Paper-Agent：AutoGen+LangGraph、仅 arXiv 单源、无评测无可观测性）。
> 本文的任务是：把散落的论文/项目/博客**收敛成一张设计图纸**，告诉你 2026 年做一个 DR 项目应该抄什么、补什么、差异化打什么。

---

## 一、总体判断（结论先行）

**Deep Research Agent 在 2025–2026 年已经从"研究 demo"收敛为一套相当稳定的工程范式**：顶层三阶段（明确需求 → 多智能体并行检索 → 聚合写作）+ 中层 supervisor-worker 编排 + 底层 ReAct 循环与状态压缩。开源生态（LangGraph ODR、gpt-researcher、DeerFlow、STORM）在架构上高度趋同，真正拉开差距的是三件**还没被做好的小事**：**引用验证、可观测性、评测**。

对你的面试项目而言，**不要试图再造一个 gpt-researcher**——那没有差异化。正确姿势是：**抄最成熟的骨架（LangGraph Open Deep Research），把精力砸在三个公认短板上**，并用一个有说服力的小评测集证明你的版本更好。

---

## 二、2026 年 Deep Research Agent 核心技术要素清单

下表是把论文、开源项目、博客三方交叉验证后，归纳出的 DR Agent 必备/加分技术要素。每条都附来源。

| # | 技术要素 | 解决什么问题 | 来源 |
|---|---|---|---|
| E1 | **Scoping / 用户澄清循环**：开跑前 LLM 判断是否需要向用户追问深度、范围、受众 | 避免跑 5 分钟发现方向跑偏 | LangChain ODR 博客；bolshchikov 拆解 |
| E2 | **Planner / 任务分解**：把 query 拆成可并行的子问题/子主题 | 单线程搜索覆盖不全 | gpt-researcher；STORM 多视角；DeerFlow Planner |
| E3 | **多视角提问（Multi-perspective）**：Planner 先输出 3–5 个对立/互补视角，每个视角单独跑分支 | 缓解单一视角偏见、提升报告平衡度 | STORM 论文/项目 |
| E4 | **Supervisor-Worker 多 Agent 编排**：supervisor 拆任务、分发给并行 worker，worker 有独立 context window | 长上下文爆炸 + 并行加速 | LangGraph ODR；LangGraph supervisor 包 |
| E5 | **Worker 内部 ReAct 循环**：search → think → compress → continue/stop | 让 worker 自己决定何时停 | LangGraph ODR；WebDancer |
| E6 | **反思 / Query 改写**：每轮检索后判断 query 太宽/太窄/跑偏，改写再搜 | 搜索路径对 query 措辞极度敏感 | RE-Searcher；DeepResearcher RL |
| E7 | **检索时机决策**：在推理中遇到不确定点才插入检索，而不是固定节奏 | 减少冗余检索、提升连贯性 | Search-o1 |
| E8 | **Reason-in-Documents / 文档内压缩**：长网页先做结构化事实提取，再进报告 | 长上下文噪声 | Search-o1 |
| E9 | **状态压缩 / 历史重建**：定期把 worker 原始轨迹压成"事实卡片"，丢弃中间思考垃圾 | 长程上下文爆炸 | IterResearch（Markovian 状态重建）；DeerFlow 中间件；LangGraph ODR compress_research |
| E10 | **递归研究树**：广度生成多 query + 深度下钻 promising branch | 发现隐藏关联 | gpt-researcher Deep Research 模式 |
| E11 | **代码执行工具**：Python REPL 做数值计算、图表、数据清洗 | DR 报告常需要数据分析 | OpenAI DR System Card；DeerFlow Coder；HF smolagents CodeAgent |
| E12 | **多搜索后端 + MCP**：Tavily/Exa/SerpAPI/Bing 可插拔，运行时切换 | 工程化、避免供应商锁定 | LangGraph ODR MCP 支持；rentierdigital 生产指南 |
| E13 | **引用聚合与回链**：报告自动带真实 URL，可点击 | 可信度 | gpt-researcher；所有开源项目默认 |
| E14 | **引用验证（Citation Faithfulness Audit）**：从报告抽引用，重新打开原文做 NLI/字符串匹配，不通过的标红或重写 | 幻觉引用是真实严重问题 | L-MARS；VeriFact-CoT；Cornell 2026 幻觉引用审计（14.6 万条/年） |
| E15 | **多模型路由 / 双模型分工**：强模型做规划，便宜模型做总结；或不同子任务路由不同模型 | 成本优化 | Perplexity；秘塔 DeepSeek-R1+自研小模型 |
| E16 | **可观测性 / Trace**：LangSmith 或 Langfuse，每一步可回放 | 调试、面试讲故事 | 所有生产级教程默认；你旧项目的痛点 |
| E17 | **评测集**：挑 10–20 道 BrowseComp/GAIA 风格题，记录轨迹长度、token、引用准确率 | 证明"我的版本更好" | BrowseComp；DeepResearchGym；DR³-Eval |
| E18 | **中间件链（横切关注点）**：重试、压缩、限流、澄清、断工具调用修补做成 middleware | 工程化、可测试 | DeerFlow 2.0 |
| E19 | **人在环 / 问题链可视化**：展示当前 plan 的 todo 树、已完成/进行中分支 | 缓解黑箱感 | Co-STORM mind map；秘塔问题链 |
| E20 | **诚实性 / Confidence 输出**：信息不足时显式承认"未找到可靠证据" | 避免强行编造 | DeepResearcher RL 涌现行为 |

> **新人提示**：E1–E13 是"做出来就能跑"的基础件，E14–E20 是"做出彩"的加分件。面试项目不必全做，优先保 E1–E6 + E9 + E14 + E16 + E17，这九件事已经能讲出一个有深度的故事。

---

## 三、3–5 种主流架构模式对比

综合 Wayland Zhang 的 2×2 框架（架构 × 能力来源）和 survey 论文的分类，2026 年的 DR Agent 可以归纳为 **5 种主流架构模式**。

### 模式 A：单 Agent + 强推理模型（上下文工程路线）
- **代表**：OpenAI Deep Research（o3）、Gemini Deep Research。
- **结构**：一个长程推理模型自己 plan → browse → code → write，循环几十步。
- **优点**：实现最简单（本质是一个带工具的 reasoning model）；跨步骤一致性好。
- **缺点**：极度依赖底座模型能力；开源复现几乎做不到同等水平；token 成本高；上下文爆炸快。
- **对你的项目**：**不要走这条**——你没有 o3 级别的模型，硬做会变成 demo。

### 模式 B：固定管线多 Agent（Planner–Executor–Publisher）
- **代表**：gpt-researcher（早期版本）、AutoResearch 类项目。
- **结构**：Planner 一次性拆子问题 → 并行 Crawler 抓资料 → Publisher 聚合。流程跑完就结束，不回头。
- **优点**：实现简单、确定性强、容易 debug。
- **缺点**：**没有反思和重规划**——如果 Planner 一开始拆错了，后面一路错到底；不能根据检索发现动态调整方向。
- **对你的项目**：这是你旧项目 Paper-Agent 接近的模式。**不要再停在这里**——固定管线是 2023 年的范式，2026 年面试讲它会显得过时。

### 模式 C：Supervisor-Worker + 嵌套图（当前开源主流）
- **代表**：LangChain Open Deep Research、DeerFlow 1.x、LangGraph 官方示例。
- **结构**：
  - 顶层图三阶段：Scope → Research → Write。
  - Research 阶段：Supervisor 拆子主题，分发给并行 Worker 子图。
  - 每个 Worker 自己跑 ReAct 循环（search → think → compress → stop），结果压缩后回传 Supervisor。
- **优点**：worker 上下文隔离（解决长程爆炸）；并行加速；typed state 可观测；supervisor 可以根据中间结果**追加任务**（这是对模式 B 的关键升级）。
- **缺点**：supervisor 自己也是 LLM，拆任务质量依赖 prompt；多一层编排，代码量上来。
- **对你的项目**：**首选骨架**。LangGraph 官方有 `langgraph-supervisor-py`，不用自己写路由。

### 模式 D：模拟多视角对话（Simulated Conversation）
- **代表**：STORM、Co-STORM。
- **结构**：先发现多个专家视角，再让 Writer Agent 与"模拟专家"多轮对话，专家基于检索回答；最后写作。
- **优点**：报告结构最平衡、最像维基百科；天然带多视角。
- **缺点**：非常烧 token；输出形态固定（长文章），不适合灵活问答。
- **对你的项目**：**不要整套搬**，但把"多视角提问"这一步借到模式 C 的 Planner 里（即 E3）。

### 模式 E：CodeAgent / 单 Agent 写代码办事
- **代表**：HF Open Deep Research（smolagents CodeAgent）。
- **结构**：Agent 不输出工具调用 JSON，而是直接写 Python 代码来组合工具。
- **优点**：表达力强（可以 for 循环、条件、串工具）；GAIA 上表现好。
- **缺点**：代码可能跑错，需要 sandbox；调试难；多 Agent 协作弱。
- **对你的项目**：作为**对比点**在 README 里提一句即可，不必采用。

### 对比表

| 模式 | 代表 | 实现难度 | 长程稳定性 | 成本 | 适合面试项目？ |
|---|---|---|---|---|---|
| A 单 Agent 强推理 | OpenAI DR | 低（依赖模型） | 高 | 极高 | ✗ 没模型 |
| B 固定管线多 Agent | 早期 gpt-researcher | 低 | 低（不反思） | 低 | △ 你旧项目已停在这 |
| **C Supervisor-Worker 嵌套图** | LangGraph ODR、DeerFlow | 中 | 高 | 中 | **✓ 首选骨架** |
| D 模拟多视角对话 | STORM | 中 | 中 | 高 | △ 借多视角，不整套搬 |
| E CodeAgent | HF Open DR | 中 | 中 | 中 | △ 作对比点 |

---

## 四、对面试项目最有价值的 5 个设计建议

> 排序按"投入产出比"——代码量小、面试讲故事分量重、且能直接对应你旧项目痛点。

### 建议 1：骨架用 LangGraph Open Deep Research（模式 C），不要自己造轮子
- **做什么**：顶层 `Scope → Research → Write` 三阶段；Research 阶段用 `langgraph-supervisor-py` 拆子任务，worker 子图跑 ReAct + compress。State 用 Pydantic typed schema。
- **为什么**：这是 2025–2026 年开源界的事实标准，面试官大概率读过；你抄得越标准，讲故事越省力。
- **解决旧项目痛点**：你旧项目是 AutoGen+LangGraph 单源、无可观测性。换成 LangGraph 原生图 + LangSmith trace，**可观测性这一项直接补上**。
- **来源**：
  - LangChain 官方博客 https://www.langchain.com/blog/open-deep-research
  - 架构拆解 https://www.bolshchikov.com/p/open-deep-research-internals-a-step
  - supervisor 模式 https://juejin.cn/post/7587903505135697970

### 建议 2：做一个 CitationVerifier Agent——这是你的差异化核心
- **做什么**：最终报告写完后，新增一个 `CitationVerifier` 节点：
  1. 从报告 AST 抽出所有 `[n]` 引用及其对应的 claim；
  2. 对每条引用，重新 fetch 原 URL，用 LLM-as-judge 或 NLI 判断"原文是否真的支持这句话"；
  3. 不通过的引用标红、在报告末尾生成"存疑引用清单"，并触发一次重写。
  - 可选加分：多模型交叉验证（让两个独立模型分别判断，不一致就升级为"存疑"）。
- **为什么**：
  - gpt-researcher、STORM、LangGraph ODR 都**只聚合 URL，不回查原文**；
  - Cornell 2026 审计发现 2025 年单年 14.6 万条幻觉引用，100+ 条通过了 NeurIPS 2025 同行评审——这是真实严重痛点；
  - L-MARS 论文已经证明这个模块可行，但开源圈没人做好。
- **解决旧项目痛点**：你旧项目仅 arXiv 单源，引用本身是 arXiv 真实链接，但**没有验证"那句话真的在论文里"**。这个模块直接补。
- **代码量**：约 100–150 行（一个节点 + 一个 prompt + 一个 NLI 调用）。
- **来源**：
  - L-MARS Citation-Faithfulness Audit https://arxiv.org/html/2509.00761
  - VeriFact-CoT（自验证不可靠，必须工具化）https://arxiv.org/html/2509.05741
  - 幻觉引用审计 https://www.alphaxiv.org/overview/2605.07723
  - 多模型交叉验证 https://www.promptquorum.com/prompt-engineering/ai-powered-research

### 建议 3：加一个"反思 + Query 改写"节点，把固定管线变成循环
- **做什么**：worker 内部每轮检索后，跑一个 `reflect(query, search_results, open_questions)` 节点：
  - 判断本轮检索结果是"太宽（噪声多）/ 太窄（信息少）/ 跑偏（与子主题无关）/ 够了（可以停）"；
  - 输出下一轮 query 或 stop 信号。
- **为什么**：
  - 你旧项目是"先搜完再写"，没有这个循环；
  - RE-Searcher 论文证明搜索轨迹对 query 措辞极度敏感，显式反思能显著降方差；
  - 这是把"固定管线（模式 B）"升级成"动态循环（模式 C）"的最小改动。
- **来源**：
  - RE-Searcher https://openreview.net/forum?id=4f4gzyD2Av
  - DeepResearcher RL 涌现的反思行为 https://arxiv.org/html/2504.03160v3
  - 秘塔"先思考后搜索" https://cloud.tencent.com/developer/news/2203477

### 建议 4：做一个最小评测集（evals/ 目录），用数据说话
- **做什么**：
  - 从 BrowseComp（1,266 题）和 GAIA 里挑 10–20 道中文面试官熟悉的题；
  - 跑你的 Agent，记录：最终答案正确率、轨迹长度（步数）、token 消耗、引用准确率（配合建议 2 的 CitationVerifier）；
  - 做成 README 里的一张表格 + 一个对比 baseline（比如裸 gpt-researcher 默认配置）。
- **为什么**：
  - 绝大多数开源 DR demo 项目**没有评测**，跑通就发；
  - 你旧项目的痛点之一就是"无评测"；
  - 面试时一张"我的项目在 BrowseComp 子集上引用准确率 92%，baseline 78%"的表格，比讲十页架构图都有说服力。
- **来源**：
  - BrowseComp https://arxiv.org/pdf/2504.12516.pdf
  - DeepResearchGym https://arxiv.org/abs/2505.19253
  - DR³-Eval（更接近真实）https://nju-link.github.io/DR3-Eval/static/DR3-Eval-latest.pdf

### 建议 5：把"研究过程"可视化，做一个问题链 UI
- **做什么**：
  - 用 Streamlit / Gradio / 简单 Web UI 展示当前 Supervisor 的 todo list（已完成/进行中/待办分支）；
  - 每个 worker 分支展开后能看到它的 query → 检索结果 → 压缩后的事实卡片；
  - 最后接报告全文 + CitationVerifier 的存疑引用高亮。
- **为什么**：
  - 你旧项目没有可观测性；
  - Co-STORM 的动态 mind map 和秘塔的"问题链可视化"都证明这是用户最买账的功能；
  - 面试现场 demo 时，**能边跑边看到研究树生长**，比最终输出一份 PDF 震撼得多。
- **来源**：
  - Co-STORM mind map https://www.skillpack.co/solutions/storm-stanford
  - 秘塔问题链 http://www.cnr.cn/tech/techgd/20251121/t20251121_527436708.shtml
  - 36 氪秘塔全链路解析 https://36kr.com/p/3973202605437448

---

## 五、不建议做的事（避免浪费时间）

1. **不要从头训 RL**：DeepResearcher、WebSailor 那条线是论文工作，面试项目讲"我读了这篇论文、把它的涌现行为写成了 prompt"就够了。
2. **不要自己写 supervisor 路由**：用 `langgraph-supervisor-py` 现成包，把时间省给 CitationVerifier。
3. **不要追求"全网浏览器操作"**：API 检索（Tavily/Exa）已经覆盖 80% 场景；浏览器自动化（Playwright）是另一个大坑，面试项目不值得。
4. **不要一开始就多搜索后端**：先接一个 Tavily（或免费的 DuckDuckGo），跑通闭环再抽象 MCP。
5. **不要做 IMRaD 式完整论文综述**：你的产出是技术设计文档和开源代码，不是学术论文。

---

## 六、一句话给你的项目定位

> "一个基于 LangGraph supervisor-worker 架构的开源 Deep Research Agent，差异化亮点是**工具化的引用验证（CitationVerifier）+ 反思式 query 改写 + 最小 BrowseComp 评测集 + 问题链可视化 UI**——补上了 gpt-researcher 和 LangGraph ODR 都没做好的引用可信度这一课。"

这句话可以直接放在 README 的第一段。
