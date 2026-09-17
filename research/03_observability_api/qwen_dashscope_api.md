# 阿里云 Qwen / DashScope API 调研笔记

> 调研时间：2026-09-16。定价随时可能调整，**以阿里云百炼官方文档为准**（文末链接）。
> 平台正式名称：**阿里云百炼（Model Studio）**，SDK/API 旧称 **DashScope**。

---

## 1. 接入方式（API Key、认证、Endpoint）

### 1.1 获取 API Key
1. 注册阿里云账号 → 开通「百炼大模型服务平台」：https://bailian.console.aliyun.com
2. 控制台 → API-KEY 管理 → 创建，得到形如 `sk-xxxxxxxxxxxxxxxx` 的 Key。
3. 设为环境变量（**全项目统一用这个变量名**）：
   ```bash
   # Windows PowerShell
   $env:DASHSCOPE_API_KEY = "sk-xxxxxxxxxxxxxxxx"
   # .env 文件
   DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxx
   ```

### 1.2 认证方式
HTTP Header：`Authorization: Bearer $DASHSCOPE_API_KEY`。

### 1.3 Endpoint（两套，推荐 OpenAI 兼容模式）

| 用途 | Endpoint |
|---|---|
| **OpenAI 兼容（推荐，LangChain 直接用）** | `https://dashscope.aliyuncs.com/compatible-mode/v1`（北京） |
| 国际版 | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| DashScope 原生 SDK | `https://dashscope.aliyuncs.com/api/v1` |
| 新版 MaaS 域名（迁移中，可选） | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |

> 对 LangChain/LangGraph 项目，**强烈建议走 OpenAI 兼容模式**：只要改 `base_url` + `api_key` + `model` 三个值，就能把 `ChatOpenAI` 切到 Qwen，无需学原生 SDK。

### 1.4 免费额度（重要，个人项目省钱）
- 开通百炼后，**每个模型独立赠送 100 万 Token** 免费额度，**自开通/模型发布起 90 天内有效**，各模型不互通。
- 嵌入模型 text-embedding-v3/v4 各送 **50 万 Token**。
- 参考：https://help.aliyun.com/zh/model-studio/new-free-quota

---

## 2. 当前可用的 Qwen 文本模型（2026 年，北京/华北2）

> 用户提到的 "qwen 3.8" 对应官方文档中的 **`qwen3.8-max`**（旗舰），确有其模型。下表为个人项目常用候选。

| 模型 ID | 定位 | 上下文 | 最大输出 | Function Calling | 结构化输出 | 输入价(≤128K) | 输出价(≤128K) |
|---|---|---|---|---|---|---|---|
| `qwen3.8-max` | 最新旗舰 | 见模型详情页 | 见详情 | 支持 | 支持 | 旗舰价（较高） | 较高 |
| `qwen3.7-max` | 旗舰 | 长上下文 | 大 | 支持 | 支持 | 较高 | 较高 |
| `qwen3.6-plus` | 高性能通用 | ~1M | 32K | 支持 | 支持 | ~2 元/M | ~12 元/M |
| `qwen-plus`（=qwen-plus-2025-12-01） | **性价比主力** | **1M** | **32768** | 支持 | 支持 | **0.8 元/M** | **2 元/M**（思考 8 元/M） |
| `qwen3.7-flash` | 低价快速 | 1M | 大 | 支持 | 支持 | **0.2 元/M** | **0.8 元/M** |
| `qwen-turbo` | 最便宜 | 131K | 8K | 支持 | 支持 | **0.3 元/M** | **0.6 元/M**（思考 3 元/M） |
| `qwen3-coder-flash` | 代码/工具调用 | 1M | 大 | 支持 | 支持 | 1 元/M | 4 元/M |
| `qwen3-235b-a22b-instruct-2507` 等开源版 | 自托管/兼容 | 256K | 32K | 支持 | 支持 | 见官网 | 见官网 |

> 阶梯计价：输入超过 128K/256K 后单价上升（如 qwen-plus：128K–256K 输入 2.4 元、输出 20 元；256K–1M 输入 4.8 元、输出 48 元）。DeepResearch 一般单次输入不会超 128K。
> 官方模型列表：https://help.aliyun.com/zh/model-studio/getting-started/models
> 完整定价：https://help.aliyun.com/zh/model-studio/model-pricing

### 选型建议（个人项目）
- **主用 `qwen-plus`**：1M 上下文、便宜、稳定支持 function calling + 结构化输出，DeepResearch 够用。
- **省钱/跑批用 `qwen-turbo` 或 `qwen3.7-flash`**。
- **面试演示讲最新能力可用 `qwen3.8-max`**（按官方示例 model 名）。
- **设计上 model 名做成配置项**（`.env` 里 `QWEN_MODEL=qwen-plus`），不要硬编码。

---

## 3. Function Calling / Tool Use

DashScope 原生 API 通过 `tools` + `tool_choice` 实现（OpenAI 风格）：

- 发起工具调用**必须**设 `result_format="message"`。
- `tools` 数组：`{type: "function", function: {name, description, parameters(JSON Schema)}}`。
- `tool_choice`：`"auto"`（默认）/ `"none"` / `{"type":"function","function":{"name":"..."}}`（强制）。思考模式模型不支持强制指定。
- `parallel_tool_calls: true` 可并行调用多个工具。
- 模型返回 `tool_calls` → 你执行工具 → 把结果作为 `role: "tool"` 消息回填 → 再次调用，构成多轮工具循环。

OpenAI 兼容模式下与 OpenAI SDK 的 `tools`/`tool_calls` **字段完全一致**，LangGraph 的 `create_react_agent` 开箱即用。

---

## 4. 结构化输出（JSON mode / structured output）

`response_format` 三选一：
- `{"type": "text"}`：普通文本（默认）。
- `{"type": "json_object"}`：输出合法 JSON（**提示词里要明确说"输出 JSON"**，否则报错）。
- `{"type": "json_schema", "json_schema": {name, schema, strict}}`：**严格按给定 JSON Schema 输出**，提示词里无需提 JSON。

qwen-plus 等主流模型均支持结构化输出。OpenAI 兼容模式对应 `response_format={"type":"json_object"}` / `pydantic` 模型（LangChain `with_structured_output` 直接可用）。

---

## 5. 流式、多轮对话

- **Streaming**：支持。原生 SDK `stream=True` + `incremental_output=True`（推荐增量输出，逐段打印）；OpenAI 兼容模式 `stream=True` 返回 SSE。
- **多轮对话**：`result_format="message"`，messages 数组按 `system → user → assistant → tool → user…` 顺序追加即可。

---

## 6. 联网搜索（DeepResearch 场景很有用）

`enable_search=true` 即可让模型在生成时参考互联网结果；`search_strategy="agent"` 支持**多轮检索 + 网页抓取**（仅 qwen3.8-max、qwen3.7-max、qwen3.5-plus/flash、qwen3-max 等），可在 Agent 外部搜索工具不可用时作为内置兜底。

---

## 7. 文本嵌入（Embedding）

| 模型 | 向量维度 | 单行 max token | 批量大小 | 价格 | 免费额度 |
|---|---|---|---|---|---|
| `text-embedding-v3` | 1024(默认)/768/512/256/128/64 | 8192 | 10 | ~0.0005 元/千 tokens（$0.07/M） | 50 万 Token / 90 天 |
| `text-embedding-v4`（Qwen3-Embedding） | 2048/1536/1024(默认)/768/512/256/128/64 | 8192 | 10 | 见官网 | 50 万 Token / 90 天 |
| `qwen3-rerank` | 重排序模型 | — | — | 见官网 | — |

- 也支持 OpenAI 兼容 embedding 接口（`/v1/embeddings`），LangChain `OpenAIEmbeddings` 改 base_url 即可。
- 注意：换维度要重建向量库，**在配置里固定 `dimensions`**（如 1024）。

---

## 8. 在 LangChain/LangGraph 中接入（推荐做法）

用 `langchain-openai` 走 OpenAI 兼容模式：

```python
import os
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model=os.getenv("QWEN_MODEL", "qwen-plus"),
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    temperature=0.7,
)
# 结构化输出
structured = llm.with_structured_output(MySchema)
# 直接喂给 create_react_agent(...)
```

嵌入同理：
```python
from langchain_openai import OpenAIEmbeddings
emb = OpenAIEmbeddings(
    model="text-embedding-v3",
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    dimensions=1024,
)
```

---

## 9. 用户需手动准备的环境变量清单

| 变量 | 说明 | 获取处 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 百炼 API Key（sk- 开头） | 百炼控制台 API-KEY 管理 |
| `QWEN_MODEL` | 聊天模型名，默认 `qwen-plus` | 自行决定 |
| `QWEN_EMBEDDING_MODEL` | 嵌入模型，默认 `text-embedding-v3` | 自行决定 |
| （可选）`DASHSCOPE_BASE_URL` | 默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` | 北京/国际不同 |

## 10. 官方文档链接
- DashScope API 参考：https://help.aliyun.com/zh/model-studio/qwen-api-via-dashscope
- 选择模型：https://help.aliyun.com/zh/model-studio/getting-started/models
- 模型定价：https://help.aliyun.com/zh/model-studio/model-pricing
- OpenAI 兼容 Chat：https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
- Base URL 总览：https://help.aliyun.com/zh/model-studio/base-url
- 文本向量化：https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api
- 新人免费额度：https://help.aliyun.com/zh/model-studio/new-free-quota
- LangChain 集成示例：https://help.aliyun.com/zh/model-studio/use-bailian-in-langchain
