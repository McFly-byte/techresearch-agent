# 选型建议：可观测性平台 + Qwen API + MCP

> 调研时间：2026-09-16。面向：个人秋招面试开源 DeepResearchAgent（Python + LangChain/LangGraph）。

---

## 1. 可观测性平台：推荐 LangSmith（免费版），Langfuse 作备选

### 结论
**默认用 LangSmith Developer 免费版做观测**；本地调试/演示怕额度或想开源自托管时，切 Langfuse。

### 理由
| 维度 | LangSmith | Langfuse（备选） |
|---|---|---|
| 免费额度 | 5k traces/月，14 天保留 | ~50k observations/月；自托管无限 |
| LangGraph 集成 | **零配置，2 个环境变量** | 需传 CallbackHandler |
| 面试关键词 | "LangSmith / LangGraph 全家桶" | "自托管开源可观测平台" |
| 演示效果 | run tree + LangGraph 状态图最佳 | 够用但朴素 |
| 开源 | 否 | 是（MIT） |

个人项目 trace 用量可控（一次 DeepResearch ≈ 1 个 trace），LangSmith 5k 额度足够；且面试现场打开 LangSmith 看板演示效果最好。

### 集成要点（用户需手动操作）
1. 注册 https://smith.langchain.com → 新建 project → 拿 `LANGCHAIN_API_KEY`（`lsv2_` 开头）。
2. 在 `.env` 中配置：
   ```bash
   LANGCHAIN_TRACING_V2=true
   LANGCHAIN_API_KEY=lsv2_xxxxxxxx
   LANGCHAIN_PROJECT=deepresearch
   # 可选：默认 https://api.smith.langchain.com
   ```
3. 业务代码**零改动**——LangChain/LangGraph 会自动把每个 LLM/工具调用发到 LangSmith。
4. 若改用 Langfuse：设 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`，并在 Agent 调用处挂 `CallbackHandler`。

### 工程建议（避免厂商锁定）
- 把"是否开启观测、用哪家"做成配置开关（`OBSERVABILITY_BACKEND=langsmith|langfuse|none`）。
- 业务代码只依赖 LangChain 标准 callback 接口，不直接 import 某家 SDK。

---

## 2. Qwen / DashScope API：推荐走 OpenAI 兼容模式

### 结论
- **模型**：聊天主用 `qwen-plus`（1M 上下文、0.8/2 元每百万 token、支持 function calling + 结构化输出）；省钱跑批用 `qwen-turbo` / `qwen3.7-flash`；演示最新能力可用 `qwen3.8-max`。
- **接入方式**：用 `langchain-openai.ChatOpenAI`，把 `base_url` 指向 `https://dashscope.aliyuncs.com/compatible-mode/v1`。**不要**用原生 dashscope SDK，方便以后换模型供应商。
- **嵌入**：`text-embedding-v3`（默认 1024 维，50 万 Token 免费）。

### 用户需手动准备的环境变量
```bash
DASHSCOPE_API_KEY=sk-xxxxxxxx          # 百炼控制台获取
QWEN_MODEL=qwen-plus                   # 可改 qwen3.8-max / qwen-turbo
QWEN_EMBEDDING_MODEL=text-embedding-v3
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

### 接入要点
- Function Calling：OpenAI 兼容模式字段与 OpenAI 一致，`create_react_agent` 直接可用。
- 结构化输出：`llm.with_structured_output(Schema)` 对应官方 `response_format=json_schema`。
- 流式：`stream=True` 直接生效。
- 省钱：新模型各送 100 万 Token（90 天内），嵌入送 50 万 Token。
- 设计上**模型名可配置**，不硬编码 "qwen 3.8"。

---

## 3. MCP：建议"轻量采用"——做一个 MCP Server 作为亮点，但不全面改造

### 结论
**采用，但控制范围**：
- 把 DeepResearch 的核心工具（网页搜索/抓取、检索）**封装成一个本地 MCP Server**（用 FastMCP / Python SDK）。
- Agent 侧通过 `langchain-mcp-adapters`（或主包 `langchain.mcp`）把它当 Tool 接入 LangGraph。
- 这样既有"MCP 标准化工具接入"的面试亮点，又不至于为两三个小工具过度工程化。

### 为什么值得
- MCP 是 2025–2026 Agent 岗高频考点；能现场用 MCP Inspector 展示自研 Server 很加分。
- 工具与编排解耦，Server 可被 Claude Desktop/Cursor 复用。
- 生态成熟：官方 Python/TS SDK 为 Tier 1，GitHub 有官方 Registry。

### 为什么不建议全面改造
- 项目小、工具少，直接写 LangChain `@tool` 更快；
- 协议仍在快速演进（2025-11-25 版），生产化要额外处理鉴权/传输。

### 集成要点
- 工具层用 FastMCP 写一个 `deepresearch-mcp-server`，暴露 `search_web`、`fetch_page` 等 Tool。
- 编排层：`MultiServerMCPClient(...).get_tools()` → 传入 `create_react_agent`。
- 调试：用 **MCP Inspector** 单独测 Server。

---

## 4. 一页纸最终选型

| 组件 | 选择 | 一句话理由 |
|---|---|---|
| LLM | `qwen-plus`（OpenAI 兼容模式） | 便宜、1M 上下文、工具调用齐全 |
| Embedding | `text-embedding-v3`（1024 维） | 免费额度够、LangChain 兼容 |
| 可观测 | **LangSmith 免费版**（备选 Langfuse） | 零配置、LangGraph 演示最佳 |
| 工具协议 | **MCP（FastMCP）**，轻量包一个 Server | 面试亮点、工具可复用 |
| 编排 | LangGraph `create_react_agent` | 与 LangSmith 同生态 |

---

## 5. 主要来源
- LangSmith 定价：https://www.langchain.com/pricing ｜ https://docs.smith.langchain.com/pricing
- Langfuse：https://langfuse.com/docs ｜ https://langfuse.com/pricing
- Arize Phoenix：https://arize.com/phoenix ｜ https://arize.com/compare/arize-vs-langsmith/
- Helicone：https://www.helicone.ai/pricing
- Braintrust：https://www.braintrust.dev/pricing ｜ https://www.braintrust.dev/docs/plans-and-limits
- AgentOps：https://www.agentops.ai ｜ https://docs.agentops.ai/v2/examples/langchain
- OpenTelemetry GenAI：https://opentelemetry.io/blog/2024/otel-generative-ai/
- 阿里云百炼 DashScope：https://help.aliyun.com/zh/model-studio/qwen-api-via-dashscope
- 百炼模型定价：https://help.aliyun.com/zh/model-studio/model-pricing
- 百炼模型列表：https://help.aliyun.com/zh/model-studio/getting-started/models
- OpenAI 兼容模式：https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
- 文本向量化：https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api
- 新人免费额度：https://help.aliyun.com/zh/model-studio/new-free-quota
- MCP 官网/规范：https://modelcontextprotocol.io ｜ https://modelcontextprotocol.org/specification/2025-11-25/basic
- MCP SDK：https://modelcontextprotocol.io/docs/sdk
- LangChain × MCP：https://www.langchain.com/blog/mcp-in-langchain-stateless-protocol-elicitation-and-more
