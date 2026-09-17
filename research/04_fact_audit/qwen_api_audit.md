# Qwen（阿里云百炼 DashScope）API 官方核验报告

> 核验日期：**2026-09-16**
> 核验方式：直接抓取阿里云官方帮助文档（help.aliyun.com / alibabacloud.com）原文，不依赖二手摘要。
> 证据等级标注：✅ 官方确认（已抓到官方原文）｜ 🟡 二手来源（仅检索摘要）｜ ⚪ 待确认（未能访问官方页）

---

## 0. 给新人的术语铺垫

- **百炼 / Model Studio**：阿里云的大模型 API 平台，对外提供千问（Qwen）及第三方模型调用。
- **DashScope**：百炼最早的一套私有 API 协议（参数塞在 `parameters` 里）。
- **OpenAI 兼容模式**：百炼另开的一套地址，请求/响应格式和 OpenAI 官方 SDK 一模一样（`/chat/completions`），方便直接套 OpenAI SDK。
- **model id**：调用时 `model=` 后面填的精确字符串，必须和官方一字不差。
- **token**：模型计费/计长的最小单位，约等于“四分之三个英文单词 / 半个到一个汉字”。
- **Function Calling / 工具调用**：模型能输出“请调用某工具、传什么参数”的结构化指令，是做 Agent 的核心能力。
- **结构化输出（json_schema）**：强制模型按你给定的 JSON 字段模板输出，不自由发挥，方便程序解析。
- **流式（stream）**：模型边生成边吐字，而不是憋到最后一次性返回。

---

## 1. 核心结论：`qwen3.8-max` 是否存在？

### ✅ 存在，且为当前旗舰之一。精确 model id 如下：

| 用途 | 精确 model id | 证据 |
|---|---|---|
| 稳定版（别名，自动指向最新快照） | **`qwen3.8-max`** | 官方模型信息页 |
| 快照（锁定版本，可复现） | **`qwen3.8-max-0902`**（别名 `qwen3.8-max-2026-09-02`） | 官方模型信息页 |
| 同系列轻量版 | **`qwen3.8-flash`** | 官方 API 参考页 |

> **重要纠错说明（证据等级 ✅）**：此前“概览页只看到 qwen3.6”的现象具有误导性。
> - “选择模型”概览页（[getting-started/models](https://help.aliyun.com/zh/model-studio/getting-started/models)，**更新于 2026-05-20**）首页只列出 `qwen3.6-max-preview / qwen3.6-plus / qwen3.6-flash`，**尚未更新**到 9 月新模型。
> - 但 DashScope API 参考页（[qwen-api-via-dashscope](https://help.aliyun.com/zh/model-studio/qwen-api-via-dashscope)，**更新于 2026-09-15**）和专门的模型信息页（[qwen3-8-max](https://help.aliyun.com/zh/model-studio/qwen3-8-max)，**更新于 2026-09-11**）都**明确、多次**出现 `qwen3.8-max`、`qwen3.8-max-0902`、`qwen3.8-flash`，并给出了调用示例代码（`model="qwen3.8-max"`）。
> - **结论：`qwen3.8-max` 真实存在，不是臆造。** 概览页只是更新滞后。

官方原文佐证（API 参考页，2026-09-15）：
> “**preserve_thinking**（默认值为 false（**qwen3.8-max/qwen3.8-flash 默认值为 true**））…… 目前支持 **qwen3.8-max、qwen3.8-max-0902、qwen3.8-flash**……”
> “**qwen3.8系列模型：默认值为 xhigh**”
> 多语言调用示例均为 `model="qwen3.8-max"`。

---

## 2. qwen3.8-max 能力与规格（✅ 官方确认）

来源：[qwen3.8-max 模型信息页](https://help.aliyun.com/zh/model-studio/qwen3-8-max)（更新 2026-09-11）

| 项目 | 官方取值 |
|---|---|
| 架构 | 2.4 万亿参数 MoE 旗舰，原生视觉理解 |
| 输入模态 | Image / Text / Video（图文视频） |
| 输出模态 | Text |
| **上下文长度（总窗口）** | **1,000,000（100 万 token）** |
| 最大输入长度 | 991,808（思考模式下 983,616） |
| **最大输出长度** | **131,072（约 13 万 token）** |
| 最大思维链（思考过程）长度 | 262,144 |
| **Function Calling（工具调用）** | ✅ 支持 |
| **结构化输出（json_schema / json_object）** | ✅ 支持 |
| **流式输出（stream）** | ✅ 支持（思考模式下**仅支持流式**，见 API 参考页） |
| 联网搜索 | ✅ 支持（北京/新加坡等地域；部分海外地域不支持） |
| 前缀续写 / 上下文缓存 / 批量推理 | ✅ 支持（批量推理仅北京/新加坡） |
| 模型调优（微调） | ❌ 不支持 |
| 发布/快照时间 | 快照 `0902` = **2026-09-02**；模型信息页 2026-09-11 上线 |

### 关键技术细节（做 Agent 必读，✅ 官方确认）
- **思考模式默认强开**：`qwen3.8` 系列 `reasoning_effort` 默认 `xhigh`；映射 `thinking_budget`：`low≈4096`、`medium≈16384`、`xhigh≈262144`；两者不能同时设置。
- **`preserve_thinking` 默认 `true`**：用 qwen3.8-max/flash 时，多轮对话必须把历史里**全部 `reasoning_content`（思考过程）原样回传**，不能拼进 `content` 字段，否则效果下降。这是和老模型最大的区别，做对话记忆层时要特别注意。
- **联网搜索 `agent` / `agent_max` 策略**：`qwen3.8-max` 支持多轮检索整合与网页抓取的高级搜索策略（仅思考模式、仅流式）。
- **限流**：北京/新加坡为动态 TPM 限流（按百炼月消费档位），RPM 较高；其他地域 RPM 30,000、TPM 5,000,000（0902 快照为 TPM 150,000）。

---

## 3. 定价（人民币，查询日 2026-09-16）

### qwen3.8-max（✅ 官方确认，来源 [模型信息页](https://help.aliyun.com/zh/model-studio/qwen3-8-max)）

| 计费项 | 价格 | 单位 |
|---|---|---|
| 输入（普通） | **¥12** | 每百万 tokens |
| 输出 | **¥36** | 每百万 tokens |
| 输入（缓存命中） | ¥1.5 | 每百万 tokens |
| 显式缓存创建 | ¥15 | 每百万 tokens |
| 显式缓存命中 | ¥1 | 每百万 tokens |
| 输入（Batch File 离线批处理） | ¥6 | 每百万 tokens |
| 输出（Batch File） | ¥18 | 每百万 tokens |

> 说明：以上为华北2（北京）/新加坡等主流地域**原价**；个别“全球”部署范围显示输入 ¥14.988 / 输出 ¥44.965（约为国际站汇率换算）。国际站美元价为 **输入 $2 / 输出 $6 每百万 token**（来源：[Alibaba Cloud 英文定价](https://www.alibabacloud.com/help/en/model-studio/model-pricing)、[qwencloud.com](https://www.qwencloud.com/models/qwen3.8-max)，🟡 与人民币价一致）。
> 官方提示：仅展示原价，限时优惠以百炼控制台为准。

### 成本直觉（给新人）
- 一次“输入约 1 万 token、输出约 2 千 token”的调研问答 ≈ 输入 ¥0.12 + 输出 ¥0.072 ≈ **¥0.2 量级**。
- 开启上下文缓存后，重复部分输入只要 ¥1.5/百万，约为原价 1/8，长对话场景务必开缓存。

---

## 4. OpenAI 兼容模式 endpoint（✅ 官方确认）

来源：[OpenAI兼容-Chat 文档](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)（更新 2026-09-15）

- **base_url**：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
- **调用地址**：`POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions`
- 各地域把 `cn-beijing` 换成 `ap-southeast-1`（新加坡）/ `us-east-1`（弗吉尼亚）/ `eu-central-1`（法兰克福）/ `ap-northeast-1`（东京）/ `cn-hongkong`（中国香港）即可。
- `{WorkspaceId}` 是你的**业务空间 ID**，在百炼控制台“业务空间详情”查看（不再是老的 `dashscope.aliyuncs.com` 裸域名）。
- 兼容 OpenAI 的 `messages / stream / tools / tool_calls / response_format / temperature / top_p` 等；`response_format` 支持 `json_object` 与 `json_schema`（可用 Pydantic/Zod 直接传）。
- 注意：`top_k`、`repetition_penalty` 等**非 OpenAI 标准参数**需放进 `extra_body`。
- Qwen-Audio **不支持** OpenAI 兼容协议，只能走 DashScope 协议。

> 兼容老域名 `https://dashscope.aliyuncs.com/compatible-mode/v1` 仍可用，但官方建议迁移到业务空间专属域名。

---

## 5. 嵌入（Embedding）模型选项与定价（✅ 官方确认）

来源：[Embedding 官方文档](https://help.aliyun.com/en/model-studio/embedding)（更新 2026-09-05）、[Synchronous API](https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api)

| 模型 | 说明 |
|---|---|
| **`text-embedding-v4`**（属 Qwen3-Embedding 系列） | 文本向量化主选 |
| `tongyi-embedding-vision-plus` | 图文多模态向量化 |
| `qwen3-rerank` | 重排序（RAG 第二步精排用） |

**text-embedding-v4 规格：**
- 向量维度可选：2048 / 1536 / **1024（默认）** / 768 / 512 / 256 / 128 / 64
- 批量大小 10；每行最大 8,192 token
- **价格：¥0.0005 / 每 1 千 token（即 ¥0.5 / 百万输入 token）**；Batch 调用 ¥0.00025/千（¥0.25/百万）
- 语言：100+（中、英、日、韩、法、西、葡、印尼、德、俄等）
- 国际站：约 $0.07 / 百万输入 token

> 嵌入模型只对输入 token 计费，不产生输出 token。

---

## 6. 新人免费额度政策（部分 ✅ 官方确认，部分 ⚪ 待控制台确认）

| 项目 | 官方说法 | 证据等级 |
|---|---|---|
| **text-embedding-v4 免费额度** | **100 万 token**，自百炼开通/模型发布/审批通过起 **90 天内有效** | ✅ [Embedding 文档](https://help.aliyun.com/en/model-studio/embedding) |
| **Agent Studio 知识库（RAG）免费额度** | 一次性 **720 小时**；新用户自开通起 **30 天内有效**，过期作废 | ✅ [Agent Studio 计费说明](https://docs.agent.bailian.aliyun.com/zh/rag/settings/billing) |
| **大语言模型（如 qwen3.8-max）新用户免费 token 包** | 官方文档未在本次抓取中给出统一数字；社区称开通即送百万级 token 体验额度 | ⚪ **待用户在百炼控制台“费用中心/额度”以实际显示为准**（🟡 二手：[CSDN 攻略](https://blog.csdn.net/weixin_29204205/article/details/158899970)） |

> 建议：注册后第一时间到百炼控制台 → 费用中心 → 额度管理，截图确认 LLM 免费 token 池的具体数量与有效期；免费额度通常只覆盖特定模型且有有效期，**不能假设 qwen3.8-max 这种旗舰一定在免费池里**。

---

## 7. 当前 Qwen3 系列可用文本模型（✅ 官方确认）

综合 API 参考页与模型信息页，当前在售/在文档中出现的 Qwen3 文本系列（精确 id，举例）：

- **qwen3.8 系列**：`qwen3.8-max`、`qwen3.8-max-0902`、`qwen3.8-flash`（最新旗舰，9 月）
- **qwen3.7 系列**：`qwen3.7-max`（及 `qwen3.7-max-2026-05-20`）、`qwen3.7-plus`、`qwen3.7-flash`
- **qwen3.6 系列**：`qwen3.6-max-preview`、`qwen3.6-plus`、`qwen3.6-flash`
- **qwen3.5 系列**：`qwen3.5-plus`、`qwen3.5-flash`、`qwen3.5-omni-plus`（全模态）
- 以及 `qwen3-max`、`qwen3-max-preview`、开源 Qwen3 系列、Qwen3-Coder 等

> 选型建议（新人）：调研/写报告这类重推理任务用 **`qwen3.8-max`**；追求成本/速度用 **`qwen3.8-flash`** 或 `qwen3.6-flash`；嵌入用 **`text-embedding-v4`**。

---

## 8. 对技术设计的影响（给 Agent 开发者）

1. **不需要“模型适配层”去猜 model id** —— `qwen3.8-max` 已官方确认，可直接硬编码为默认；但建议把 model id 做成配置项，方便随时切到 `qwen3.8-flash` 降本。
2. **必须处理 `preserve_thinking=true`**：多轮 Agent 若直接调 qwen3.8-max，要把历史 `reasoning_content` 原样回传，否则效果下降；用 OpenAI SDK 时思考内容走 `reasoning` 字段。
3. **1M 上下文 + 131k 最大输出**：足以支撑长报告生成，但注意“输出含思维链”——计费的输出 token 包含 thinking，实际“正文”可能远小于 131k。
4. **RAG 栈**：`text-embedding-v4`（1024 维默认）+ `qwen3-rerank` 是官方推荐组合。
5. **接入方式**：优先 OpenAI 兼容模式（`/compatible-mode/v1`），生态最省改造成本。

---

## 官方 URL 清单
- 模型选择（概览）：https://help.aliyun.com/zh/model-studio/getting-started/models
- DashScope API 参考：https://help.aliyun.com/zh/model-studio/qwen-api-via-dashscope
- OpenAI 兼容 API：https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
- qwen3.8-max 模型信息/定价/规格：https://help.aliyun.com/zh/model-studio/qwen3-8-max
- 嵌入模型：https://help.aliyun.com/en/model-studio/embedding ；https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api
- 国际站定价：https://www.alibabacloud.com/help/en/model-studio/model-pricing
- Agent Studio 知识库计费：https://docs.agent.bailian.aliyun.com/zh/rag/settings/billing
