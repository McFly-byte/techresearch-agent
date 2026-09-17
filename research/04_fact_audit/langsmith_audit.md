# LangSmith 免费额度官方核验报告

> 核验日期：**2026-09-16**
> 核验方式：直接抓取 LangChain 官方定价页与官方文档原文。
> 证据等级：✅ 官方确认｜ 🟡 二手来源｜ ⚪ 待确认

---

## 0. 给新人的术语铺垫

- **LangSmith**：LangChain 官方出的“Agent 可观测 + 评估”平台。你写的 Agent 每跑一步（调了哪个 LLM、传了什么、工具返回什么）都会作为一条 **trace（追踪记录）** 送到 LangSmith，在网页上可视化、可打分、可回归测试。
- **trace（追踪）**：Agent 一次完整执行 = 一条 trace；里面可以包含很多步（多次 LLM 调用、工具调用）。
- **seat（席位）**：账号下能登录使用的人数。
- **evaluation / eval（评估）**：自动给 Agent 的输出打分，回归对比改前改后。
- **LangGraph**：LangChain 出的“图式 Agent 编排框架”，把 Agent 画成节点+边的状态机。

---

## 1. 核心结论：此前“5k traces/月、1 席位、14 天保留”的说法是否属实？

### ✅ 三项全部属实（官方确认）

| 此前说法 | 官方核验结果 | 证据 |
|---|---|---|
| 免费版 = **5k traces / 月** | ✅ 属实：Developer 计划含 **5k base traces / 月**，超出后按量付费 | 官方定价页 |
| 免费版 = **1 席位** | ✅ 属实：**Maximum of 1 seat (free)** | 官方定价页 |
| 免费版数据保留 **14 天** | ✅ 属实：**base traces 保留 14 天**（extended traces 才保留 400 天） | 官方定价 FAQ |

> 官方原文（[docs.smith.langchain.com/pricing](https://docs.smith.langchain.com/pricing)）：
> “**Developer** — \$0 / seat per month … Up to **5k base traces / mo**, then pay-as-you-go … **1 seat**”
> “What is the difference between a base trace and an extended trace? **Base traces have a shorter retention period of 14 days** and cost \$2.50 per 1k traces. Extended traces have a longer retention period of **400 days**…”

---

## 2. Developer 免费版完整额度清单（✅ 官方确认）

来源：[www.langchain.com/pricing](https://www.langchain.com/pricing) 与 [docs.smith.langchain.com/pricing](https://docs.smith.langchain.com/pricing)

| 维度 | Developer（免费，$0/席/月） |
|---|---|
| 价格 | **$0 / seat / 月**，之后按量付费 |
| 席位 | **1 个**（个人） |
| 基础 trace 量 | **5,000 条 / 月**，超出按量付费（base trace \$2.50 / 1k） |
| **数据保留期** | **base trace 14 天**；可付费升级为 extended trace（400 天，\$5/1k） |
| Tracing（追踪调试） | ✅ 包含 |
| **Online & offline evals（在线+离线评估）** | ✅ **免费版就支持**（“Online and offline evals”） |
| Prompt Hub / Playground / Canvas（提示词优化） | ✅ 包含 |
| Annotation queue（人工标注反馈） | ✅ 包含 |
| Monitoring & alerting（监控告警） | ✅ 包含 |
| Fleet agent（任务型 agent） | 1 个，最多 50 次运行/月 |
| 社区支持 | Community Forum（免费） |
| 部署（Deployment） | ❌ 不含（N/A） |
| 计费方式 | 月度自助（monthly, self-serve） |

> **对新人的关键提示**：
> - “14 天保留”是指**免费的 base trace 只存 14 天**。做长期回归测试时，重要 trace 需要另存（或付费升 extended）。
> - **免费版就带 evaluation（evals）能力**，无需升级到 \$39/月 的 Plus 版即可做自动打分回归——这对个人开发者很友好。
> - 安全/SSO：Developer 仅支持 Google / GitHub 登录；Plus 才有自定义 SSO。

---

## 3. LangGraph 状态可视化是否支持？（✅ 基本确认）

- LangSmith 会把 LangGraph Agent 的每次执行记录为 trace，并在 UI 中展示**节点/边的调用顺序与中间步骤**（官方定价页特性：“Real-time streaming of intermediary steps and final output”）。
- **LangSmith Studio** 是官方免费的可视化界面，可在本地开发时可视化测试 Agent（来源：LangSmith Studio 文档，🟡/✅）。
- 因此 **LangGraph 的执行轨迹（状态流）在免费版 trace 中即可可视化**；更深度的“Agent 作为长服务部署”（Deployment）才需要 Plus 版。

---

## 4. 接入所需环境变量的精确含义（✅ 官方确认）

来源：[Trace with LangChain](https://docs.smith.langchain.com/how_to_guides/tracing/trace_with_langchain)、[Create an account and API key](https://docs.langchain.com/langsmith/create-account-api-key)、[官方 Support 文章](https://support.langchain.com/articles/3567245886)

| 环境变量 | 取值示例 | 精确含义 |
|---|---|---|
| `LANGCHAIN_TRACING_V2` | `true` | **总开关**。设为 `true` 才把 trace 发给 LangSmith。（这是经典写法） |
| `LANGCHAIN_API_KEY` | `lsv2_pt_xxx` | **你的 LangSmith 密钥**。在 smith.langchain.com → Settings → API Keys 创建，形如 `lsv2-...`。用来鉴权。 |
| `LANGCHAIN_PROJECT` | `my-research-agent` | **项目名**。trace 按“项目”分组；不设则归入默认的 `default` 项目。建议按实验/模块命名，方便区分。 |
| `LANGCHAIN_ENDPOINT`（可选） | `https://api.smith.langchain.com` | 服务地址，默认即该值；私有化/欧盟区才需要改。 |

> **⚠️ 重要：新旧命名并存（新人最容易踩坑）**
> 官方新文档（2026）已改用新名字：
> - `LANGSMITH_TRACING=true`（替代旧的 `LANGCHAIN_TRACING_V2`）
> - `LANGSMITH_API_KEY=...`（替代旧的 `LANGCHAIN_API_KEY`）
> - `LANGSMITH_PROJECT=...`（替代旧的 `LANGCHAIN_PROJECT`）
> 两套变量名目前**都能工作**。老教程/老代码用 `LANGCHAIN_*`，新官方推荐 `LANGSMITH_*`。做新项目建议直接用新名字。
>
> Windows PowerShell 临时设置示例：
> ```powershell
> $env:LANGSMITH_TRACING="true"
> $env:LANGSMITH_API_KEY="lsv2_pt_你的key"
> $env:LANGSMITH_PROJECT="deep-research-agent"
> ```

---

## 5. 个人开发者注册流程（✅ 官方确认）

来源：[LangSmith Account 文档](https://docs.langchain.com/langsmith/admin)（更新 2026-09-15）、[Billing 文档](https://docs.langchain.com/langsmith/billing)

1. 打开 **smith.langchain.com** 注册。
2. **登录方式：Google、GitHub 或邮箱** 三选一。
3. **✅ 注册不需要信用卡**（官方原文：“Sign up at smith.langchain.com **(no credit card required)**”）。
4. 注册后进 Settings → API Keys → Create API Key，拿到 `lsv2-...` 密钥。
5. **关键限制**：未绑卡的个人组织**被限制在 5,000 traces/月**。想超过额度需在 Billing 里“Add card”绑卡，之后超出部分按量付费（官方原文：“Personal organizations are limited to 5,000 traces per month **until a credit card is added**”）。

> 也就是说：**完全免费、无需信用卡即可开始，且天然就是 5k/月上限**；绑卡不是注册前提，而是“想超额/用付费功能”才需要。

---

## 6. 对技术设计的影响（给 Agent 开发者）

1. **预算上可放心用免费版起步**：5k traces/月、含 evals、含 LangGraph 轨迹可视化，个人开发/调试完全够。
2. **注意 14 天保留**：做基线回归实验时，重要分数建议**自己落盘存档**（CSV/数据库），不要只指望 LangSmith 网页留 14 天。
3. **环境变量直接抄新写法** `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT`，但要知道老代码用的是 `LANGCHAIN_*`。
4. **注册零门槛**：GitHub/Google 一键登录，无需信用卡，当天可接入。

---

## 官方 URL 清单
- 定价（营销页）：https://www.langchain.com/pricing
- 定价（文档/FAQ，含保留期）：https://docs.smith.langchain.com/pricing
- 账号与注册（无需信用卡）：https://docs.langchain.com/langsmith/admin
- 计费（5k 上限与绑卡）：https://docs.langchain.com/langsmith/billing
- 环境变量/接入：https://docs.langchain.com/how_to_guides/tracing/trace_with_langchain ；https://docs.langchain.com/langsmith/create-account-api-key
- 环境变量设置（官方 Support）：https://support.langchain.com/articles/3567245886
