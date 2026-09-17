# MCP（Model Context Protocol）协议调研笔记

> 调研时间：2026-09-16。MCP 迭代很快，以官方规范为准（文末链接）。

---

## 1. MCP 是什么

**Model Context Protocol（MCP）** 是 Anthropic 于 2024 年 11 月开源的开放协议，目标是**标准化"LLM 应用（Host/Client）"与"外部工具/数据源（Server）"之间的连接方式**。

打个比方：USB-C 统一了硬件接口，MCP 想统一"AI 应用 ↔ 工具"的接口。以前每个 Agent 框架要为每个工具（GitHub、数据库、文件系统）写一套适配代码；现在只要工具方实现一次 MCP Server，任何支持 MCP 的 Client（Claude Desktop、Cursor、LangGraph、VS Code 等）都能直接用。

- 底层消息：**JSON-RPC 2.0**。
- 2025 年 11 月起，协议由 **Agentic AI Foundation（Linux 基金会旗下）** 接管治理，不再是 Anthropic 单方项目。

---

## 2. 核心概念

| 概念 | 含义 |
|---|---|
| **Host（宿主）** | 用户-facing 的 AI 应用，如 Claude Desktop、IDE、你的 DeepResearch Agent。 |
| **Client（客户端）** | Host 内部与某个 Server 1:1 连接、做能力协商的组件。 |
| **Server（服务端）** | 对外暴露能力的进程，可本地（stdio）可远程（Streamable HTTP/SSE）。 |
| **Tool（工具）** | 可被模型调用的函数（如 `search_web`、`query_db`），对应 Function Calling。 |
| **Resource（资源）** | 只读数据，如文件、数据库记录，供 Client 读取上下文。 |
| **Prompt（提示模板）** | Server 预置的提示词模板。 |
| 其他 | Sampling（Server 反向请求 Client 的 LLM）、Roots（Client 暴露的目录）、Elicitation（向用户索取输入）、Logging。 |

一个 Server 可以同时暴露 Tool + Resource + Prompt。

---

## 3. 2025–2026 发展现状

- **规范版本**：2025-03-26（加入 OAuth 2 授权）→ 2025-06-18 → **2025-11-25（当前最新正式版）**，新增无状态模式、Elicitation 等。
- **官方 SDK 语言支持**（Tier 1 = 一等公民）：
  - Tier 1：**TypeScript、Python、C#（与微软合作）、Go**
  - Tier 2：Java、Rust
  - Tier 3：Swift
- **生态成熟度**：
  - 2024-11 发布时约 100 个 Server；到 2026 年 1 月第三方目录收录已 **7,800+** 个 Server。
  - **GitHub 于 2025-10 推出官方 MCP Registry**，可发现/安装/管理 Server。
  - OpenAI、Google、Microsoft 均已宣布支持 MCP。
- **知名 MCP Server（面试可点名）**：
  - 官方参考：`filesystem`、`fetch`（抓网页转 markdown）、`memory`（知识图谱）、`everything`（测试用）
  - 常用：`github`、`slack`、`playwright`（浏览器自动化，微软官方维护）、`postgresql`、`brave-search`、`context7`（最新库文档）、`puppeteer`
- **官方调试工具**：**MCP Inspector**（可视化查看某个 Server 暴露了哪些 Tool/Resource 并试调用）。

---

## 4. 对 Deep Research Agent 的价值

1. **工具接入标准化**：搜索、爬虫、数据库、论文检索等工具，统一按 MCP Tool 暴露；换工具只改 Server，不改 Agent 编排代码。
2. **可复用性**：写一次 MCP Server，Claude Desktop / Cursor / 你自己的 LangGraph Agent 都能连；面试时"我的搜索工具是标准 MCP Server，可被任意 MCP Client 复用"是亮点。
3. **面试亮点**：MCP 是 2025–2026 年 Agent 岗高频考点，能讲清 Host/Client/Server 三层架构、stdio vs Streamable HTTP 传输、与 Function Calling 的关系，明显加分。
4. **与 LangGraph 解耦**：工具层和编排层分离，方便单测（用 MCP Inspector 测 Server）。

---

## 5. 与 LangChain / LangGraph 的集成

官方库 **`langchain-mcp-adapters`**（2026-09 起已并入主包 `langchain.mcp`，基于 **FastMCP** 构建）：

```python
import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

async def main():
    client = MultiServerMCPClient({
        "search": {"command": "uvx", "args": ["mcp-server-fetch"], "transport": "stdio"},
    })
    tools = await client.get_tools()          # MCP Tool → LangChain Tool
    agent = create_react_agent("qwen-plus", tools)
    await agent.ainvoke({"messages": [("user", "总结某网页")]})

asyncio.run(main())
```

要点：
- `MultiServerMCPClient` 同时连多个 MCP Server，把它们的 Tool 聚合成 LangChain Tool 列表。
- 支持 stdio（本地子进程）和 Streamable HTTP（远程 Server）两种 transport。
- 官方文档：https://modelcontextprotocol.io ｜ LangChain MCP 文档见 langchain.com 博客与 LangChain 中文文档。

---

## 6. 适用场景与局限性

**适用**
- 工具/数据源种类多、可能反复更换；
- 希望工具能被多个 Agent 框架/客户端复用；
- 想接社区现成 Server（GitHub、Playwright、Notion 等）。

**局限 / 注意**
- 协议仍在快速演进，版本间有 breaking change（无状态模式、OAuth 较新）；
- 本地 stdio 模式每个 Client 起一个子进程，调试要懂进程管理；
- 远程 Server 生产化要自己解决鉴权（OAuth）、限流、可观测；
- 对一个**只有两三个内置工具的小型面试项目**，直接写 LangChain `@tool` 比套 MCP 更简单——MCP 是"锦上添花"，不要为了 MCP 而 MCP。

---

## 7. 官方链接
- 官网/规范：https://modelcontextprotocol.io
- 规范（2025-11-25）：https://modelcontextprotocol.org/specification/2025-11-25/basic
- SDK 列表：https://modelcontextprotocol.io/docs/sdk
- 架构概览：https://modelcontextprotocol.io/docs/learn/architecture
- 参考 Server 仓库：https://github.com/modelcontextprotocol/servers
- GitHub MCP Registry 介绍：https://github.blog/ai-and-ml/generative-ai/how-to-find-install-and-manage-mcp-servers-with-the-github-mcp-registry/
- LangChain MCP 集成博客：https://www.langchain.com/blog/mcp-in-langchain-stateless-protocol-elicitation-and-more
- Anthropic 发布公告：https://www.anthropic.com/news/model-context-protocol
