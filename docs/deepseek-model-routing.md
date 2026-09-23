# DeepSeek 模型路由

本项目支持 `LLM_PROVIDER=deepseek`。2026-09-22 通过当前账号的官方 `/models`
端点实时确认可用模型为 `deepseek-v4-pro` 与 `deepseek-flash`；配置不继续依赖会被
服务端映射的旧模型别名。

| 阶段 | 模型 | 选择理由 |
|---|---|---|
| 当前确定性 Planner | 无 LLM | 避免为可解释拆解增加调用 |
| Fact extraction | `deepseek-flash` | 高并发结构化抽取，控制延迟与成本 |
| Citation verifier | `deepseek-flash` | 判断任务短、可并行，需稳定格式 |
| Final synthesis | `deepseek-flash` | v4-pro 实测耗尽输出预算且超时，flash 更符合硬时限 |
| 非官方 rubric Judge | `deepseek-flash` | 与回答模型区分用途，控制评测成本 |
| 可选显式深度推理 | `deepseek-v4-pro` | 保留能力，但不进入当前有硬时限的 benchmark 主链 |

`DeepSeekProvider.with_thinking()` 不发送无效的 Qwen `enable_thinking` 参数；它同时
切换模型并发送 DeepSeek 的 `thinking.type=enabled/disabled`。当前主链显式关闭 thinking，
避免隐藏推理先耗尽输出预算、导致 `content` 为空。LangSmith usage metadata 会记录 provider、
model 和 Token。抽取、NLI 与 Judge 还会启用 OpenAI 兼容 JSON mode。DeepSeek Judge 只作为新的非官方预评口径，不与历史
`qwen-nonofficial` 分数直接混合。

固定 Core4 v8 的在线复验在四题上达到 `4 completed / 0 failed`，相较 v7 的
`3 / 1` 消除了空正文失败；平均 Token 从 `45,394.25` 降至 `35,195.00`。不过
非官方 Judge 均分只有 `0.075777`、pass rate 仍为 0%，因此该路由目前只证明了
输出契约和运行稳定性，不代表研究质量已经达标。

环境变量：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_MODEL_FAST=deepseek-flash
DEEPSEEK_REASONER_MODEL=deepseek-v4-pro
DEEPSEEK_VERIFIER_MODEL=deepseek-flash
DEEPSEEK_SYNTHESIS_MODEL=deepseek-flash
DEEPSEEK_JUDGE_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_REQUEST_TIMEOUT_SECONDS=120
```
