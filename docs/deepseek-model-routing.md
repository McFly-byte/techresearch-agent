# DeepSeek 模型路由

本项目支持 `LLM_PROVIDER=deepseek`。2026-09-22 通过当前账号的官方 `/models`
端点实时确认可用模型为 `deepseek-v4-pro` 与 `deepseek-flash`；配置不继续依赖会被
服务端映射的旧模型别名。

| 阶段 | 模型 | 选择理由 |
|---|---|---|
| Planner、复杂研究决策、反思 | `deepseek-v4-pro` | 优先复杂推理与覆盖规划 |
| Fact extraction | `deepseek-flash` | 高并发结构化抽取，控制延迟与成本 |
| Citation verifier | `deepseek-flash` | 判断任务短、可并行，需稳定格式 |
| Final synthesis | `deepseek-v4-pro` | 需要跨证据与 coverage matrix 综合 |
| 非官方 rubric Judge | `deepseek-flash` | 与回答模型区分用途，控制评测成本 |

`DeepSeekProvider.with_thinking()` 不发送 Qwen 专属的 `enable_thinking` 参数；开启与
关闭通过上述两个模型 ID 切换。LangSmith usage metadata 会记录实际返回的 provider、
model 和 Token。DeepSeek Judge 只作为新的非官方预评口径，不与历史
`qwen-nonofficial` 分数直接混合。

环境变量：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_MODEL_FAST=deepseek-flash
DEEPSEEK_VERIFIER_MODEL=deepseek-flash
DEEPSEEK_SYNTHESIS_MODEL=deepseek-v4-pro
DEEPSEEK_JUDGE_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_REQUEST_TIMEOUT_SECONDS=120
```
