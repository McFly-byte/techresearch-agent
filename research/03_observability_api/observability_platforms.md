# Agent 可观测性平台（AgentOps）对比调研

> 调研时间：2026-09-16。定价与免费额度可能随时调整，以官网为准（文末附链接）。
> 适用对象：个人秋招面试开源 DeepResearchAgent 项目，技术栈 Python + LangChain/LangGraph。

---

## 0. 先搞懂几个概念（给 Agent 开发新人）

| 概念 | 通俗解释 |
|---|---|
| **Trace（追踪）** | 一次完整的"用户提问 → Agent 最终回答"全过程。DeepResearch 场景下可能持续几十秒到几分钟。 |
| **Span（跨度）** | Trace 内部的一个步骤，比如"调了一次 LLM""调了一次搜索工具""做了一次向量检索"。Trace = 一棵树，Span 是树上的节点。 |
| **Token / Cost** | 每次 LLM 调用消耗的输入/输出 token 数与折算成本。Agent 容易" runaway 循环"，成本监控很重要。 |
| **Evaluation（评估）** | 给一次 Trace 打分：回答对不对、有没有用对工具、检索到的文档相关不相关。分"人工标注"和"LLM-as-judge"（让另一个模型打分）。 |
| **Replay / Replay Debugging** | 把一次 Agent 运行录下来，事后逐步回放，看每一步为什么这么决策。 |
| **Callback（LangChain）** | LangChain 在执行每个节点时会回调注册的 handler，平台靠这个自动抓取 Span，不用改业务代码。 |
| **OpenTelemetry (OTel)** | CNCF 的通用可观测性标准，定义了 Trace/Metrics/Logs 三类信号。`gen_ai.*` 语义约定（semantic conventions）是 LLM 场景的标准字段。 |

一句话：**可观测性平台 = 把 Agent 的"思考过程"录成一棵树，让你能回放、能打分、能看钱花在哪。**

---

## 1. 横向对比总表

> 免费额度按 2026-09 官网公开信息整理。

| 平台 | 开源 | 免费额度（Cloud） | 付费起步 | LangChain/LangGraph 集成 | 架构特点 |
|---|---|---|---|---|---|
| **LangSmith** | 否（闭源 SaaS） | Developer $0：5k traces/月，1 席位，14 天保留 | Plus $39/席位/月（10k traces） | **原生、零配置**（2 个环境变量） | LangChain 官方出品 |
| **Langfuse** | 是（MIT） | Hobby：~50k observations/月；自托管**无限** | Pro $59/月 | 官方 Callback 集成 | 自托管需 ClickHouse+PG+Redis |
| **Arize Phoenix** | 是（ELv2） | 自托管**完全免费无限**；AX Free 25k spans/月 | AX Pro $50/月 | OTel/OpenInference 自动埋点 | 单容器 + SQLite 即可跑 |
| **Helicone** | 是（Apache-2.0） | Hobby：10k 请求/月，1GB 存储 | Pro $79/月 | LangChain provider 走代理 | **代理/网关**架构 |
| **Braintrust** | 否 | Starter $0：$10 额度 + 1GB 数据 + 10k scores，14 天保留 | Pro $249/月 | 官方 SDK | 偏 **Evaluation** 而非追踪 |
| **AgentOps (agentops.ai)** | SDK 开源（MIT） | Basic $0：5000 events/月 | Pro $40/月 | LangChainCallbackHandler | 2 行代码接入 |
| **OTel + GenAI 语义约定** | 是 | 自建采集后端，无 SaaS 免费额度 | 看后端 | LangChain 已原生发 OTel span | 标准、无厂商锁定 |

---

## 2. 逐个平台详评

### 2.1 LangSmith（LangChain 官方，重点）

- **核心能力**：Trace 树查看（run tree）、Playground 在线调试、Datasets & Evaluations（离线评估集）、Prompt 管理与版本、监控面板、LangGraph Studio 集成。
- **语言/框架**：Python/JS SDK；框架无关，但对 LangChain/LangGraph 体验最好。
- **定价**（查询时间 2026-09）：
  - Developer：$0，1 席位，**5,000 base traces/月**，数据保留 14 天。
  - Plus：$39/席位/月，10,000 traces/月，超出约 $0.5/1k traces。
  - Enterprise：定制，支持自托管（需商务合同）。
- **集成复杂度**：**最低**。注册 → 拿 API Key → 设两个环境变量即可，无需改代码：
  ```bash
  export LANGCHAIN_TRACING_V2=true
  export LANGCHAIN_API_KEY=lsv2_xxx
  export LANGCHAIN_PROJECT=deepresearch
  ```
- **UI**：Run 树视图非常直观，能看到每次 LLM 调用的 prompt/completion、token 用量、耗时；在线 evaluate、标注队列齐全。
- **优点**：与 LangGraph 结合最紧（LangGraph Studio 可视化状态图）；面试时"用过 LangSmith"是简历关键词；开箱即用。
- **缺点**：闭源；免费额度只有 5k traces/月（一次 DeepResearch 可能几十次 LLM 调用，trace 消耗快）；14 天数据保留；自托管要 Enterprise。
- **官网**：https://smith.langchain.com ｜ 定价 https://www.langchain.com/pricing ｜ 文档 https://docs.smith.langchain.com

### 2.2 Langfuse（开源，重点）

- **核心能力**：Trace/observation 树、Prompt 管理、Datasets & Scores（打分/人工标注）、LLM-as-judge、Dashboard；v3 SDK 已发布。
- **语言/框架**：Python/JS SDK；LangChain 用 LangChain Callback 自动采集。
- **定价**（查询时间 2026-09）：
  - **Cloud Hobby：免费，约 50k observations/月**，自托管完全无限（MIT，无 feature gate）。
  - Pro：$59/月（约 1M observations）。
  - 自托管：Docker Compose 一键起，但生产需要 PostgreSQL + ClickHouse + Redis/Valkey + 对象存储，建议 4C16G。
- **集成方式**（LangChain）：
  ```python
  from langfuse.langchain import CallbackHandler
  handler = CallbackHandler()  # 读 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
  # 传给 LangChain/LangGraph 的 callbacks
  ```
- **UI**：trace 时间线、token/成本统计、session 视图、annotation 队列都有；视觉上比 LangSmith 朴素些。
- **优点**：开源可自托管（面试讲"我自托管了 Langfuse"加分）；免费额度比 LangSmith 大 10 倍；无厂商锁定。
- **缺点**：自托管栈偏重；LangGraph 状态图可视化不如 LangSmith Studio 原生。
- **官网**：https://langfuse.com ｜ 文档 https://langfuse.com/docs ｜ LangChain 集成 https://langfuse.com/integrations/frameworks/langchain

### 2.3 Arize Phoenix（开源）

- **核心能力**：本地优先的 trace + eval + 数据集实验；UMAP 嵌入聚类/漂移可视化；基于 **OpenTelemetry + OpenInference** 语义约定。
- **定价**：Phoenix 本体开源（**ELv2 协议**，免费但禁止当托管服务转售），单条 `phoenix` 命令或 `docker run` 即可起，SQLite 存储，**无用量限制**。商业版 Arize AX：Free 25k spans/月，Pro $50/月。
- **集成**：`pip install arize-phoenix`，通过 OTel auto-instrument 自动抓 LangChain/LlamaIndex/OpenAI 调用；也有 `phoenix.ai` 装饰器手动埋点。
- **优点**：本地零成本、零外部依赖、数据不出机器；天然对接 OTel 标准；eval/实验能力强。
- **缺点**：ELv2 协议比 MIT 严格；UI 偏工程师风格，团队协作/SaaS 弱于 Langfuse Cloud；Agent 多轮 session 视图略弱。
- **官网**：https://arize.com/phoenix ｜ 对比 https://arize.com/compare/arize-vs-langsmith/

### 2.4 Helicone

- **核心能力**：AI Gateway（OpenAI 兼容代理）+ 观测 + **缓存**（语义缓存）+ 限流 + 成本报表。
- **定价**：Hobby 免费 10k 请求/月、1GB、1 席位；Pro $79/月起。
- **集成**：改 base_url 把请求打到 Helicone 代理，或用 LangChain Helicone provider。
- **优点**：代理架构，不改代码即可观测；缓存能省重复调用成本。
- **缺点**：代理架构有额外延迟；对 LangGraph 这种深层多步 Agent，trace 树粒度不如 callback 方案；eval/标注能力弱。
- **官网**：https://www.helicone.ai ｜ 定价 https://www.helicone.ai/pricing

### 2.5 Braintrust

- **核心能力**：**以 Evaluation 为中心**——prompt/数据集/实验/scorer/CI 集成；trace 是配套。
- **定价**：Starter 免费（$10 额度、1GB 处理数据、10k scores、14 天保留、无限用户）；Pro $249/月。
- **优点**：做"LLM-as-judge 离线评测集"体验一流，Notion/Stripe/Vercel 在用。
- **缺点**：偏贵；免费额度对日常调试 trace 偏小；自托管仅 Enterprise。
- **官网**：https://www.braintrust.dev ｜ 定价 https://www.braintrust.dev/pricing

### 2.6 AgentOps（agentops.ai）

- **核心能力**：**Agent 专用**——session replay（逐步回放执行图）、400+ 模型成本追踪、prompt 注入/数据外泄检测、benchmark。
- **定价**：Basic 免费 5000 events/月；Pro $40/月。
- **集成**：Python/TS SDK 两行接入；LangChain 用 `LangchainCallbackHandler`；原生支持 CrewAI/AutoGen/Agno。
- **优点**：专为 Agent 设计，replay 体验好，对新人友好；MIT SDK 开源。
- **缺点**：平台较年轻；eval 生态不如 LangSmith/Langfuse；免费额度小。
- **官网**：https://www.agentops.ai ｜ LangChain 示例 https://docs.agentops.ai/v2/examples/langchain

### 2.7 OpenTelemetry + LLM 追踪方案

- **是什么**：OTel 是 CNCF 标准，2024 年底起推出 **GenAI semantic conventions**（`gen_ai.request.model`、`gen_ai.usage.input_tokens`、`gen_ai.system` 等），LangChain/LlamaIndex/OpenAI SDK 已原生或自动埋点。
- **怎么做**：应用侧用 OTel SDK 导出 span → 后端可选 Langfuse（接收 OTel）、Phoenix、Jaeger、Tempo、Datadog、Grafana 等。
- **优点**：**无厂商锁定**；一次埋点，多家后端；面试讲"我遵循 OTel GenAI 语义约定"很专业。
- **缺点**：要自己搭后端和看板，对个人项目偏重。
- **参考**：https://opentelemetry.io/blog/2024/otel-generative-ai/

---

## 3. 推荐（个人面试项目）

**首选：LangSmith（Developer 免费版）作为默认观测后端。**
理由：
1. 与 LangGraph 零配置集成，简历关键词强；
2. UI 演示效果最好（run tree + LangGraph 状态图），面试现场可直接打开演示；
3. 5k traces/月对个人开发+演示够用（DeepResearch 单次运行约 20–80 次 LLM 调用 ≈ 1 个 trace，5k 个 trace 足够）。

**备选/增强：Langfuse Cloud Hobby（50k observations/月）** 作为免费额度更大的开源选项，或 Phoenix 本地自托管用于"数据不出本机"。

工程建议：**代码层用 LangChain 原生 OTel/Callback 接口做抽象，环境变量可切换 LangSmith / Langfuse / Phoenix 后端**，不要把某家 SDK 写死在业务代码里。详见 `recommendation.md`。
