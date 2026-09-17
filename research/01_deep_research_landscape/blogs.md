# 技术博客与行业分析调研（2025–2026）

> 调研时间：2026-09-16。只收录对**架构设计、最佳实践、趋势判断**有实质信息量的文章；纯新闻稿、纯产品发布稿不收。
> 每条记录：标题、作者/出处、链接、核心观点。

---

## 1. 框架官方博客（最权威）

### 1.1 Open Deep Research — LangChain 官方博客（2025-07-16）
- **作者**：LangChain 团队
- **链接**：https://www.langchain.com/blog/open-deep-research
- **核心观点**：
  - Deep Research 是 2025 年最火的 agent 应用形态，OpenAI / Anthropic / Perplexity / Google 都有产品。
  - 开源方案必须**简单 + 可配置**：用户自带模型、自带搜索工具、自带 MCP server。
  - 架构上明确给出 **Supervisor + 子 agent** 分工：Supervisor 判断 brief 能否拆成独立子主题，分给并行 worker；worker 只关注自己子主题，用独立 context window。
- **对本项目**：这是你新项目骨架的"官方参考"，可以直接引用。

### 1.2 Introducing Deep Research: The Open Source Alternative — GPT Researcher 官方博客（2025-02-26）
- **作者**：Assaf Elovic / gpt-researcher 团队
- **链接**：https://docs.gptr.dev/blog/2025/02/26/deep-research
- **核心观点**：
  - Deep Research 的核心是**递归研究树**：每层生成多个 query 做广度探索，promising 分支递归下钻；async/await 并行跑多条路径。
  - 与"一次性 plan→search→write"相比，递归树能发现隐藏关联。
- **对本项目**：递归树的"广度+深度"讲法可直接借用。

### 1.3 RAG is Dead, Long Live Agentic Retrieval — LlamaIndex 官方博客（2025-05-29）
- **作者**：LlamaIndex 团队
- **链接**：https://www.llamaindex.ai/blog/rag-is-dead-long-live-agentic-retrieval
- **核心观点**：
  - 传统"一次检索一次生成"的 RAG 已死；未来是 **agentic retrieval**——LLM 自己决定查哪个子索引、用 chunk 还是 file-level、要不要 rerank、要不要再查一次。
  - Composite retriever + auto_routed retrieval 是落地形态。
- **对本项目**：这篇是讲"为什么需要 Deep Research"的理论背书。面试时可以用来对比你旧项目（固定 RAG）和新项目（agentic retrieval）的范式差异。

### 1.4 How OpenAI, Gemini, and Claude Use Agents to Power Deep Research — ByteByteGo（2025-12-12）
- **作者**：ByteByteGo Newsletter
- **链接**：https://blog.bytebytego.com/p/how-openai-gemini-and-claude-use
- **核心观点**：
  - **OpenAI DR**：以 o3 为中心的单 agent，先澄清再自主多步执行。
  - **Perplexity DR**：迭代检索循环 + 混合模型路由（不同子任务用不同模型）。
  - **Gemini DR**：类似思路，但深度整合 Google 搜索生态。
- **对本项目**：面试讲"单 Agent vs 多 Agent、模型训练 vs 上下文工程"两个维度时的直接素材。

---

## 2. 架构深度拆解（必读）

### 2.1 Open Deep Research Internals: A Step-by-Step Architecture Guide — bolshchikov.com（2025-11-02）
- **作者**：bolshchikov
- **链接**：https://www.bolshchikov.com/p/open-deep-research-internals-a-step
- **核心观点**：
  - 任何 DR agent 都由三部分组成：**Scoping（明确需求）→ Research（执行检索）→ Final Report（写作）**。
  - Scoping 阶段的用户澄清循环经常被低估，但它对最终报告质量影响巨大。
  - 自己设计 DR 时，每个阶段都要决定"什么时候停"——这是最容易出 bug 的地方。
- **对本项目**：这篇是我读过对 LangChain ODR 拆解最清楚的一篇，建议直接对照源码读一遍。

### 2.2 Open Deep Research 代码笔记 — hype08.github.io（2025-10-08）
- **链接**：https://hype08.github.io/gradual-notes/thoughts/Open-Deep-Research
- **核心观点**：
  - **嵌套 StateGraph** 是关键：顶层图管三阶段，Research 子图管 supervisor-worker，每个 worker 又是 ReAct 子图。
  - **Typed state with Pydantic** 让节点间通信显式，避免隐式耦合。
  - 这种"嵌套隔离"让 worker 可以独立开发测试。
- **对本项目**：直接抄它的 state schema 设计。

### 2.3 Autoresearch: Autonomous Agentic Research Loops — calwoo.github.io（2026-09 更新）
- **链接**：https://calwoo.github.io/notes/papers/autoresearch/index.html
- **核心观点**：
  - 用一张流程图把 gpt-researcher 的 planner-executor-publisher 模式画清楚。
  - 指出 2026 年 3 月 gpt-researcher 已达 25.7k star，是采用最广的开源文献综合系统。
- **对本项目**：快速理解 planner-executor 模式的最短路径。

### 2.4 GPT Researcher: The Open Deep Research Agent Reviewed — andrew.ooo（2026-08-13）
- **链接**：https://andrew.ooo/posts/gpt-researcher-deep-research-agent-review/
- **核心观点**：
  - 29k+ star、Apache-2.0、2023-05 创建、2026-07 仍活跃。
  - Provider 无关、真实 citation、递归 deep dive。
  - 缺点：固定管线、动态重规划弱。
- **对本项目**：第三方独立评测，比官方博客客观。

### 2.5 Deep Research with LangGraph: A Production Setup — rentierdigital.xyz（2026-06-18）
- **链接**：https://rentierdigital.xyz/blog/langgraph-deep-research
- **核心观点**：
  - 生产环境的图拓扑就是 planner → researcher → writer 三节点。
  - **MCP server 支持（2025 Q3 加入）**让运行时切换搜索后端成为可能——这是工程化的关键。
  - 状态里用 list field 累积 researcher 结果，writer 一次性消费。
- **对本项目**：MCP 支持是 2026 年的标配，新项目应该把搜索后端抽象成 MCP tool。

### 2.6 Building a Deep Research Agent with LangGraph and Exa — Sid Bharath（2025-08-25）
- **链接**：https://sidbharath.com/blog/build-deep-research-agent-langgraph/
- **核心观点**：
  - LangGraph 把工作流建模为状态机，每个节点处理一个方面，共享 state 对象自动流动。
  - 对新手友好：比裸写 while 循环管 messages 稳得多。
- **对本项目**：入门教程级，适合快速上手。

---

## 3. 中文技术社区（面向国内读者，讲故事更接地气）

### 3.1 解读 Deep Research — 稀土掘金（2025-11-08）
- **链接**：https://juejin.cn/post/7569898158835499049
- **核心观点**：
  - Deep Research 是从 "Reasoning" 到 "Research with Reasoning" 的转型。
  - **DR 不是单一模型**，而是由规划、检索、状态化执行、综合多个独立交互模块构成。
- **对本项目**：中文讲清楚"DR 不是模型而是系统"的最好文章之一。

### 3.2 第 27 章 Deep Research — Wayland Zhang《AI Agent 开发实战》
- **链接**：https://waylandz.com/ai-agent-book/%E7%AC%AC27%E7%AB%A0-Deep-Research/
- **核心观点（非常重要）**：
  - DR 设计空间用两个维度切：
    - **架构**：单 Agent（靠长程推理）vs 多 Agent（靠分工协调）
    - **能力来源**：模型训练（RL 让模型"学会"研究）vs 上下文工程（Prompt 设计"引导"模型研究）
  - 四个组合：单 Agent+RL（OpenAI DR）、单 Agent+上下文工程（早期 demo）、多 Agent+RL、多 Agent+上下文工程（开源主流）。
- **对本项目**：这个 **2×2 矩阵**是 synthesis 里讲架构分类的最佳框架，直接借用。

### 3.3 字节跳动开源了一款 Deep Research 项目 — 火山引擎开发者社区/博客园（2025-06-24）
- **链接**：https://www.cnblogs.com/volcengine-developer/articles/18946105
- **核心观点**：
  - 字节 DeerFlow 用 LangGraph 快速搭 ReAct 子 agent + Multi-Agent Supervisor 设计模式。
  - LangGraph 官方提供 `langgraph-supervisor-py` 默认实现，不必自己写 supervisor 路由。
- **对本项目**：直接用 `langgraph-supervisor` 包，省掉 supervisor 路由逻辑。

### 3.4 别再让单体 Agent 烧 Token 了：LangGraph 多智能体实战指南 — 稀土掘金（2025-12-26）
- **链接**：https://juejin.cn/post/7587903505135697970
- **核心观点**：
  - **Supervisor 模式**是多 Agent 协作最稳的模式：一个主控分发，多个专家执行，主控汇总。
  - 单体 Agent 在复杂任务中容易"迷失"——上下文越长，有效推理越差。
- **对本项目**：为什么需要多 Agent 而不是单 Agent，这篇讲得最通俗。

### 3.5 AI 赋能深度研究：秘塔 AI 搜索"深度研究"模块全方位解析 — CSDN（2025-07-22）
- **链接**：https://blog.csdn.net/yuntongliangda/article/details/149522059
- **核心观点**：
  - 秘塔在数据清洗、索引结构、检索算法、推理调度多层面优化。
  - 多轮推理 + 资料整合的速度能做到 2–3 分钟。
- **对本项目**：了解国内工业界怎么做"快速 DR"。

### 3.6 AI 搜索会不会取代传统搜索？秘塔 AI 搜索全链路解析 — 36 氪（2026-09-07）
- **链接**：https://36kr.com/p/3973202605437448
- **核心观点**：
  - 秘塔"深度研究"模式把自然语言问题自动拆解，**先思考框架、后整合搜索网络**，多线迭代追搜。
  - 本质是从"被动检索网页"到"主动构建知识网络"。
- **对本项目**：面试讲产品视角时引用。

---

## 4. 框架对比与趋势

### 4.1 A Comparative Architectural Analysis of LLM Agent Frameworks: LangChain, LlamaIndex, AutoGPT in 2025 — Uplatz（2025-11-20）
- **链接**：https://uplatz.com/blog/a-comparative-architectural-analysis-of-llm-agent-frameworks-langchain-llamaindex-and-autogpt-in-2025/
- **核心观点**：
  - **LlamaIndex Workflows（2025-06 发布）**：事件驱动、async-first，与 LangGraph 的状态机路线不同。
  - 实际高级应用中流行**融合架构**：LlamaIndex 管数据层（检索、rerank），LangGraph 管控制层（agent 编排）。
- **对本项目**：你旧项目用 AutoGen+LangGraph，新项目可以讲"为什么选 LangGraph 而非 LlamaIndex Workflows"——状态机比事件驱动更适合 debug。

### 4.2 The Pendulum Swing of LLM Frameworks — deepagentsdk.dev（2026-01-01）
- **链接**：https://deepagentsdk.dev/blog/26-01-01-the-pendulum-swing-of-llm-frameworks
- **核心观点**：
  - LangGraph（2024 初）专门解决传统 DAG 框架做不了的事：**循环、多轮、自纠错**。
  - LlamaIndex Workflows（2024-08）跟进事件驱动。
  - Agent 需要循环做自纠错和迭代精化，这是共识。
- **对本项目**：讲"为什么用图而不是 DAG"。

### 4.3 AI-Powered Research: Tools, Hallucination Rates, and Verification Workflows — PromptQuorum（2026-08-29）
- **链接**：https://www.promptquorum.com/prompt-engineering/ai-powered-research
- **核心观点**：
  - **多模型交叉验证**：让 GPT/Claude/Gemini 同时回答同一问题，独立模型很少幻觉出完全相同的虚假引用——这是统计意义上的去幻觉。
  - 当三个独立模型对一条引用达成一致时，三者同时幻觉同一作者/期刊/卷号/年份的概率可忽略。
  - 100+ 幻觉引用通过了 NeurIPS 2025 同行评审——引用验证不是可选项。
- **对本项目**：给你的 CitationVerifier 模块加一个"多模型交叉验证"策略，论文/博客双引用。

---

## 5. 评测与可观测性

### 5.1 BrowseComp: a benchmark for browsing agents — OpenAI 官方（2025-04-10）
- **链接**：https://openai.com/index/browsecomp/ （官方公告）
- **核心观点**：
  - SimpleQA 已饱和，BrowseComp 测的是"持续在网上找难找到、纠缠信息"的能力。
  - 1,266 道题开源在 OpenAI evals GitHub。
- **对本项目**：面试项目的 evals/ 目录直接拿 BrowseComp 子集。

### 5.2 Introducing Parallel: Web Search Infrastructure for AIs — parallel.ai（2025-08-14）
- **链接**：https://parallel.ai/blog/introducing-parallel
- **核心观点**：
  - 用 BrowseComp 随机 100 题做测量，发现搜索后端质量对 DR 最终分影响巨大。
- **对本项目**：搜索后端选型（Tavily vs Exa vs SerpAPI vs Bing）是工程权衡点，可以做成可切换配置。

---

## 6. 博客侧结论速览（给 synthesis 用）

1. **三阶段骨架（Scope→Research→Write）已成共识**，多份独立博客交叉验证。
2. **Supervisor-worker 是多 Agent 协作的事实标准**，LangGraph 官方有现成包。
3. **可观测性（LangSmith/Langfuse）不是可选**：所有生产级教程都默认接 trace。
4. **引用验证在博客层面被反复点名是痛点**，但没有开源项目给出干净的解法——你的机会。
5. **国内视角（秘塔/掘金/36氪）强调"双模型分工 + 问题链可视化"**，这是面向中国面试官讲故事的好素材。
6. **2×2 设计空间（架构 × 能力来源）**来自 Wayland Zhang 的书，是面试讲架构分类的最佳框架。
