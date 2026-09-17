# 开源 Deep Research 项目调研（2025–2026）

> 调研时间：2026-09-16。Star 数为各来源在 2026 年 6–9 月的快照，会持续变化，引用时以 GitHub 实时数据为准。
> 重点回答：每个项目**架构长什么样、有什么亮点、有什么坑、我们能借鉴什么**。

---

## 1. 全景速览表

| 项目 | 机构 | Star 量级 | 架构 | 默认检索 | 适合借鉴点 |
|---|---|---|---|---|---|
| **gpt-researcher** | assafelovic | ~28–29k | Planner–Crawler–Publisher 固定管线 + 递归研究树 | Tavily | 递归广度+深度；真实引用聚合 |
| **STORM / knowledge-storm** | Stanford OVAL | ~10k+ | 两阶段：多视角模拟对话研究 → 大纲写作 | Bing / 9 种后端 | 多视角提问；研究/写作分离 |
| **LangChain Open Deep Research** | langchain-ai | ~12k | LangGraph 嵌套 StateGraph：Scope→Research→Write，supervisor + 并行 researcher 子图 | Tavily + MCP | supervisor-worker 模式；typed state；可观测 |
| **HF Open Deep Research** (hungs629) | HuggingFace | ~28k（含 smolagents 生态） | smolagents CodeAgent，写 Python 代码调工具 | text web browser | 代码即动作；GAIA 55.15% |
| **David Zhang Open Deep Research** | 社区 (TypeScript) | ~19k | 简洁 TS 实现，递归子问题 | Tavily / 多搜索后端 | 代码可读性极好，适合读源码入门 |
| **DeerFlow** | 字节跳动 | ~75k（2.0） | LangGraph：Coordinator + Planner + Researcher + Coder + Reporter | 多搜索 + MCP | 中间件链（middleware）；sandbox；2.0 进化为 SuperAgent harness |
| **Alibaba-NLP/DeepResearch (Tongyi)** | 阿里通义 | ~12.5k | 开源 DR Agent | — | 工业界复现参考 |
| **Search-o1** | LinkedIn AI | ~3k+ | ReAct + 推理中插入检索 | 内部 | Reason-in-Documents 模块 |

---

## 2. gpt-researcher（assafelovic/gpt-researcher）

- **GitHub**：https://github.com/assafelovic/gpt-researcher
- **Star**：约 28–29k（2026 年中）；Apache-2.0；Python；2023 年 5 月创建，至今活跃维护。
- **核心架构**：
  1. **Planner Agent**：把用户 query 拆成 m 个子问题（sub-questions），构成"客观看待该主题所需的视角"。
  2. **Crawler Agent（并行）**：每个子问题触发一个 crawler，去网上抓资料；对抓到的每个资源做"是否相关"过滤、再 summarize。
  3. **Publisher**：把所有 summary 聚合成最终报告，带真实引用。
  4. **Deep Research 模式（2025-02 加入）**：递归研究树——每个节点生成多个 query 做**广度探索**， promising 分支再**递归下钻**；async/await 并行跑多条研究路径。
- **关键技术亮点**：
  - Provider 无关（OpenAI / Anthropic / Google / Groq / Ollama / 任何 OpenAI 兼容端点）。
  - 引用聚合：最终报告自动带 20+ 真实 citation，并有 source tracking。
  - 与 Tavily（联合创始人 Rotem Weiss 共建）深度绑定——Tavily 是为 agent 设计的搜索 API。
- **局限性**：
  - 早期版本是**固定管线**（plan → scrape → aggregate），不是真正的图，动态重规划能力弱。
  - 没有显式的"反思/重写 query"节点；递归深度靠配置参数，不是模型自己判断。
  - 评测主要是自报的 cost/latency，缺少 BrowseComp/GAIA 级基准。
  - 引用验证基本没有（聚合时带 URL，但不回查原文是否真支持那句话）。
- **可借鉴设计**：
  - **递归研究树**的"广度生成 query + 深度递归 promising branch"模式，代码量小但讲出来很有故事感。
  - **Planner 输出"客观所需视角"**这个 prompt 思路：不是"列出子问题"，而是"要客观回答这个主题，需要覆盖哪些方面"——能显著提升报告平衡性。
- **参考链接**：
  - 官方 Deep Research 介绍：https://docs.gptr.dev/blog/2025/02/26/deep-research
  - 架构博客：https://docs.gptr.dev/blog/building-gpt-researcher
  - 第三方深度评测：https://andrew.ooo/posts/gpt-researcher-deep-research-agent-review/

---

## 3. STORM / knowledge-storm（stanford-oval/storm）

- **GitHub**：https://github.com/stanford-oval/storm
- **Star**：~10k+；NAACL 2024 论文；2025-01 发布 knowledge-storm v1.1（litellm 集成）。
- **核心架构（两阶段）**：
  1. **Research 阶段**：
     - 先用 LLM 生成**多视角（multi-perspective）专家角色**（如"历史学家视角""技术史视角""批评者视角"）。
     - 模拟 **Writer Agent 与 Expert Agent 多轮对话**——Expert 基于网页检索回答 Writer 的追问，Writer 据此更新对主题的理解并继续追问。
  2. **Writing 阶段**：
     - 基于累积材料生成分层大纲（hierarchical outline）。
     - 分节写作，每节带引用；最后生成 lead 段并 polish。
- **Co-STORM（协作版，2024-09）**：
  - 加入 **Moderator Agent**：从 retriever 发现但前几轮没用上的信息里生成新的启发式问题。
  - 维护**动态思维导图（mind map）**作为人机共享的概念空间。
  - 支持人在环（HITL）：用户可随时注入提示或观察专家辩论。
- **关键技术亮点**：
  - **多视角提问**直接缓解"单一视角偏见"。
  - 研究和写作显式分离，避免边搜边写导致的结构漂移。
  - 支持 9 种检索后端（Bing、Google、Brave、Vector DB 等）。
  - 不同阶段可用不同模型：question asker 用便宜模型，outline 和 article gen 用强模型。
- **局限性**：
  - 定位是"生成维基百科式长文章"，不是"回答研究问题"——输出结构固定。
  - 多轮模拟对话很烧 token，速度慢。
  - 没有浏览器操作能力，纯 API 检索。
- **可借鉴设计**：
  - **多视角专家角色**：在你的 planner 节点加一步"生成 3–5 个对立/互补视角"，每个视角单独跑一条 worker 分支。
  - **研究/写作分阶段**：State 里分 `research_phase` 和 `writing_phase`，不要让一个节点同时干两件事。
  - **动态 mind map** 作为可观测性 UI——面试 demo 直接展示"当前研究树长什么样"。
- **参考链接**：
  - 仓库：https://github.com/stanford-oval/storm
  - DeepWiki 架构解读：https://deepwiki.com/stanford-oval/storm
  - Co-STORM 说明：https://www.skillpack.co/solutions/storm-stanford

---

## 4. LangChain Open Deep Research（langchain-ai/open_deep_research）

- **GitHub**：https://github.com/langchain-ai/open_deep_research （博客 https://www.langchain.com/blog/open-deep-research ，2025-07-16）
- **Star**：~12k；Python；基于 LangGraph。
- **核心架构（嵌套 StateGraph）**：
  - **顶层图三阶段**：`Scope → Research → Write`
    1. **Scoping**：用户澄清循环（LLM 判断是否需要向用户追问）→ 生成 research brief。
    2. **Research**：内部是 supervisor-worker 子图。Supervisor 判断 brief 能否拆成独立子主题，分发给并行 researcher 子 agent，每个 worker 有**独立 context window**。
    3. **Write**：聚合所有 research 结果，生成最终报告。
  - **每个 researcher 子图**自己又是一个 ReAct 循环：search → think → compress → continue/stop；带 `compress_research` 节点做状态压缩。
- **关键技术亮点**：
  - **嵌套图 + 独立 context**：worker 之间上下文隔离，supervisor 只看到压缩后的 summary——这是解决长程上下文爆炸的工业级做法。
  - **Typed state（Pydantic schema）**：状态显式、类型安全，可观测性好。
  - **MCP 支持**：2025 Q3 后可运行时切换搜索后端。
  - **模型/工具可插拔**：自带 LangSmith tracing，每一步都能回放。
- **局限性**：
  -  supervisor 自己也是 LLM，子任务拆分质量依赖 prompt；拆得不好会重复或遗漏。
  - 默认用 Tavily，免费额度有限。
  - 引用验证依然弱（靠模型自己带 citation，不回查）。
- **可借鉴设计（这是你新项目最应该抄的骨架）**：
  - **顶层 Scope → Research → Write 三段式**，几乎是 2025 年 DR 的事实标准。
  - **Supervisor + 并行 worker**，worker 自带 `compress` 节点——直接对应 IterResearch 论文的状态重建思想。
  - **Pydantic typed state**：面试讲"我们的 state schema 长什么样"比讲"我们用了 LangGraph"高级得多。
  - **LangSmith/Langfuse 可观测性**：你旧项目的痛点就是无可观测，新项目第一版就把 trace 接好。
- **参考链接**：
  - 官方博客：https://www.langchain.com/blog/open-deep-research
  - 架构拆解：https://www.bolshchikov.com/p/open-deep-research-internals-a-step
  - 代码笔记：https://hype08.github.io/gradual-notes/thoughts/Open-Deep-Research
  - Go 移植版（同样架构说明）：https://pkg.go.dev/github.com/smallnest/langgraphgo/showcases/open_deep_research

---

## 5. HuggingFace Open Deep Research（hungs629/open_deep_research，smolagents 系）

- **GitHub**：hungs629/open_deep_research（HF 生态）；~28k star 量级（含 smolagents 生态）。
- **核心架构**：基于 **smolagents CodeAgent**——agent 不输出"调用哪个工具的 JSON"，而是**直接写 Python 代码**来调用工具（浏览、滚动、搜索、下载文件、算数）。
- **关键数据**：GAIA validation 55.15%，是开源复现里最早打出 GAIA 分数的项目之一。
- **关键技术亮点**：
  - **Code-as-action**：让模型写 Python 来组合工具，比 function calling 表达力强（可以 for 循环、条件判断、串多个工具）。
  - 2025 年 2 月 OpenAI DR 发布后 **24 小时内开源**，社区影响力巨大。
- **局限性**：
  - CodeAgent 写的代码可能跑错，需要 sandbox；调试成本高。
  - 多 Agent 协作弱，更像"一个强 agent 写代码办事"。
- **可借鉴设计**：
  - 如果你想在面试里讲"工具调用范式"，CodeAgent vs Function Calling 是一个很好的对比点。
  - **GAIA 评测**：跑一下它的 GAIA 分数作为 baseline。
- **参考链接**：https://developer.volcengine.com/articles/7468990523257126953 ；对比评测 https://dreaming.press/posts/gpt-researcher-vs-open-deep-research.html

---

## 6. David Zhang Open Deep Research（TypeScript）

- **GitHub**：dzhng/open-deep-research（社区常称 "David Zhang Open Deep Research"）；~19k stars；MIT；TypeScript。
- **核心架构**：简洁的递归子问题分解：顶层生成子问题 → 每个子问题递归跑同样流程 → 聚合。
- **关键技术亮点**：代码**可读性极高**，是学习 DR 实现的最佳入门源码——几百行就能看懂整个循环。
- **局限性**：功能相对简单，没有 supervisor-worker，没有中间件，生产化程度低。
- **可借鉴设计**：
  - **读源码第一站**：在动手前先读这个项目，把最小闭环搞清楚。
  - 它的 MCP server 包装版也很有名，展示了"DR 作为工具被别的 agent 调用"的形态。
- **参考链接**：https://www.everydev.ai/tools/open-deep-research/llms.txt ；https://skywork.ai/skypage/en/unlocking-agentic-ai-deep-dive/1978341034315915264

---

## 7. DeerFlow（bytedance/deer-flow）

- **GitHub**：https://github.com/bytedance/deer-flow ；~75k stars（2.0，2026-03 重写）；MIT。
- **核心架构（1.x，Deep Research 版）**：基于 LangGraph 的多 Agent：
  - **Coordinator**：入口，工作流生命周期管理。
  - **Planner**：任务拆解与规划；判断上下文是否充分，不够就继续驱动研究团队，够了就进报告。
  - **Researcher**：网页搜索 + 爬虫 + MCP。
  - **Coder**：Python REPL 做数据分析。
  - **Reporter**：聚合写报告。
- **DeerFlow 2.0（2026-03）**：从 DR agent 进化为 **SuperAgent harness**——重写为中间件链（middleware pipeline），包括：
  - DanglingToolCallMiddleware（修补中断的工具调用）
  - 上下文自动摘要
  - TodoList 计划模式
  - Sub-agent 并发限流
  - 用户澄清拦截
  - 异步记忆更新
  - 图像注入（视觉模型）
- **关键技术亮点**：
  - **中间件链**思路值得抄：把"重试、压缩、限流、澄清"这些横切关注点做成 middleware，而不是散在节点里。
  - **Sandbox + memory + sub-agents** 三件套齐全。
- **局限性**：2.0 已经超出 Deep Research 范畴，学习曲线陡；对面试项目来说，1.x 的五 Agent 架构更值得参考。
- **可借鉴设计**：
  - **Coordinator + Planner + Researcher + Coder + Reporter** 五角色划分——比 LangGraph ODR 的三阶段更细，适合讲"职责分离"。
  - **Planner 判断"上下文够不够"**这个显式节点，就是 RE-Searcher 论文里的反思机制的工程化。
  - **Middleware 链**：你的项目可以做一个 `RetryMiddleware`、`CompressionMiddleware`、`RateLimitMiddleware`，面试讲"横切关注点"加分。
- **参考链接**：
  - 官网：https://deerflow.tech/?id=DeerFlow
  - 2.0 升级解读：https://developer.volcengine.com/articles/7622159746254307391
  - 1.x 架构解析：https://blog.csdn.net/gitblog_00851/article/details/160303875

---

## 8. Alibaba-NLP/DeepResearch（通义）

- **GitHub**：https://github.com/Alibaba-NLP/DeepResearch ；~12.5k stars（2025-09 快照）。
- **定位**：通义实验室开源的"leading open-source deep research agent"，工业界复现 OpenAI DR 路线。
- **可借鉴点**：作为国内工业界参考，看它怎么处理中文检索、怎么接通义万相搜索 API。
- **链接**：https://blog.csdn.net/u014390502/article/details/151954136

---

## 9. 商业系统的开源侧分析（Perplexity / 秘塔 / OpenAI）

### 9.1 Perplexity Deep Research
- **架构特征**（来自 ByteByteGo、技术拆解 gist）：
  - **迭代式信息检索循环**：不是一次搜完，而是根据新发现持续调整 query。
  - **混合模型路由**：不同子任务路由到不同模型——总结用一个模型，搜索解读用另一个；2026 年 Perplexity Computer 已扩展到 20+ 模型路由。
  - **三层记忆**：单次 run 内的 scratchpad、会话级 context、用户 pin 的 Spaces（持久证据库）。
- **借鉴**：
  - **混合模型路由**可以做成你项目的一个配置：planner 用强模型，summarizer 用便宜模型——直接讲成本优化。
  - **用户 pin 证据**（Spaces）是一个很用户友好的设计。
- **链接**：https://blog.bytebytego.com/p/how-openai-gemini-and-claude-use ；https://gist.github.com/Co-Messi/bfcfb39eede5c6bc2fadd2c04139a136

### 9.2 秘塔 AI 搜索（深度研究模式）
- **架构特征**（来自 36 氪、腾讯云、央广网报道）：
  - **双模型架构**：DeepSeek R1 负责"先思考后搜索"——构建思考框架、拆解研究步骤；秘塔自研小模型负责定向抓取和资料整合。
  - **问题链可视化**：把搜索规划过程的"问题链"打开给用户看，缓解黑箱感。
  - 2025-11 接入 MiniMax M2 做推理 + 交错思维链。
- **借鉴**：
  - **双模型分工**（强推理模型做规划、小模型做执行）是成本最优解，适合面试讲"生产化权衡"。
  - **问题链可视化**：你的 demo UI 直接展示当前 plan 的 todo list 和已完成/进行中分支，这是秘塔被用户好评的点。
- **链接**：https://36kr.com/p/3973202605437448 ；https://cloud.tencent.com/developer/news/2203477 ；http://www.cnr.cn/tech/techgd/20251121/t20251121_527436708.shtml

---

## 10. 横向对比：对面试项目最有用的几个判断

1. **骨架首选 LangGraph Open Deep Research**：它的 Scope→Research→Write + supervisor-worker + typed state 已经是 2025–2026 的事实标准，直接抄骨架能省 80% 设计时间。
2. **多视角提问抄 STORM**：在 planner 节点加一步，低成本提升报告平衡度。
3. **递归研究树抄 gpt-researcher**：在 worker 内部做 depth-first 递归下钻。
4. **中间件链抄 DeerFlow 2.0**：把压缩、重试、限流做成可插拔 middleware，讲工程化。
5. **引用验证没有现成开源做好**——这是你的差异化空间（详见 synthesis.md）。
6. **可观测性**几乎所有开源项目都依赖 LangSmith/Langfuse；新项目第一版就接好 trace，比后期补容易得多。
