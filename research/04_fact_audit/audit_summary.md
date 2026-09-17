# 事实审计总结（Audit Summary）

> 审计日期：**2026-09-16**
> 审计对象：此前调研中的三个关键事实声明
> 证据等级图例：✅ 官方确认（已抓取官方原文）｜ 🟡 二手来源｜ ⚪ 待用户/官网确认

---

## 一、审计结论总览

| # | 此前调研的声明 | 审计结论 | 证据等级 |
|---|---|---|---|
| 1 | 百炼 DashScope 上存在 `qwen3.8-max` | ✅ **属实**。精确 id：`qwen3.8-max`（快照 `qwen3.8-max-0902` / `qwen3.8-max-2026-09-02`），同系列 `qwen3.8-flash` | ✅ 官方确认 |
| 2 | LangSmith 免费版 = 5k traces/月、1 席位、14 天保留 | ✅ **三项全部属实**；且免费版即含 online/offline evals，注册**无需信用卡** | ✅ 官方确认 |
| 3 | DeepResearch Bench II 可作为核心评测集 | ✅ **属实且已公开**（论文+代码+rubrics），但**非商用许可（CC BY-NC-SA 4.0）**，需联网+LLM judge 成本 | ✅ 官方/论文确认 |

---

## 二、哪些被证实（与此前一致）

### 任务 1 · Qwen API
- ✅ `qwen3.8-max` **真实存在**，不是误传。它是 2.4 万亿参数 MoE 旗舰，**100 万 token 上下文、131k 最大输出**。
- ✅ **Function Calling、结构化输出（json_schema）、流式**均官方支持。
- ✅ 定价（北京/新加坡等）：**输入 ¥12 / 输出 ¥36 每百万 token**；缓存命中 ¥1.5；国际站 $2 / $6。
- ✅ OpenAI 兼容 endpoint 存在：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
- ✅ 嵌入主模型 `text-embedding-v4`：**¥0.5/百万输入 token，送 100 万 token / 90 天**。

### 任务 2 · LangSmith
- ✅ Developer 免费版：**$0、1 席位、5k base traces/月、base trace 保留 14 天**（extended 400 天）。
- ✅ 免费版**就支持 evaluation（在线+离线）**与 LangGraph 轨迹可视化。
- ✅ 注册：Google/GitHub/邮箱，**无需信用卡**；未绑卡天然就是 5k/月上限。

### 任务 3 · DeepResearch Bench II
- ✅ 真实存在：arXiv:2601.08536（2026-01），132 任务 / 22 领域 / 9,430 二元 rubric，LLM-as-judge，含人机一致性校验。
- ✅ 代码与 rubrics 已开源：github.com/imlrz/DeepResearch-Bench-II。

---

## 三、哪些被修正（与此前不一致或需补充）

1. **“概览页只看到 qwen3.6，怀疑 qwen3.8 不存在” → 修正：qwen3.8 确实存在。**
   - “选择模型”概览页（2026-05-20）只列到 qwen3.6，但 **API 参考页（2026-09-15）与专门的模型信息页（2026-09-11）明确支持 qwen3.8 系列**。原因是概览页更新滞后，并非模型不存在。**技术文档无需设计“待确认 model id”的兜底，但仍建议把 model id 做成配置项。**

2. **LangSmith 免费版细节补充/修正**：
   - “14 天保留”**只针对 base trace**；付费升级 extended 才 400 天。
   - 环境变量存在**新旧两套命名**：老教程 `LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY / LANGCHAIN_PROJECT`；新官方推荐 `LANGSMITH_TRACING / LANGSMITH_API_KEY / LANGSMITH_PROJECT`，两者当前都生效。
   - 注册“无需信用卡”属实，但**不绑卡 = 自动锁 5k traces/月**（这正是免费版上限）。

3. **DeepResearch Bench II 重要约束补充**：
   - 许可证是 **CC BY-NC-SA 4.0（非商用）**，商用产品评测需注意授权。
   - 评测**需联网**且论文警告**防来源文章泄漏**（建议搜索层屏蔽原始专家文章）。
   - 它是“研究报告质量”评测，**不含代码/工程任务**；纯技术选型里的“跑通实现”测不到，需另配 SWE-bench 类。

---

## 四、仍待确认项（⚪，不要当作既定事实）

| 待确认项 | 原因 | 建议动作 |
|---|---|---|
| GitHub 仓库 star 数 / 最后更新 / 确切文件格式（JSON/JSONL）与目录结构 | github.com 对本工具 robots.txt 拦截，未直读 | 浏览器打开 https://github.com/imlrz/DeepResearch-Bench-II 看 README |
| 百炼 LLM（qwen3.8-max）新用户免费 token 包的**具体数量与有效期** | 官方文档未在本次抓取中给出统一数字 | 登录百炼控制台 → 费用中心 → 额度管理截图确认 |
| LangSmith 免费版对**深度 LangGraph Studio 本地调试**的具体限制 | 官网确认有免费 Studio，但本地调试细节未逐字核 | 接入时实测 |

---

## 五、对技术设计文档的最终建议

1. **模型层**：默认 `qwen3.8-max`（配置项可切 `qwen3.8-flash` 降本）；用 OpenAI 兼容地址 `/compatible-mode/v1`；务必处理 `preserve_thinking=true` 的历史 `reasoning_content` 回传。
2. **可观测层**：LangSmith Developer 免费版即可起步（5k traces/月含 evals）；注意 14 天保留，重要基线自己落盘。
3. **评测层**：以 DeepResearch Bench II 为离线回归主评测（先跑子集、防泄漏、预算 LLM judge 成本）；非商用授权需留意。
4. **RAG 层**：`text-embedding-v4`（1024 维）+ `qwen3-rerank`。

---

## 附：四份明细文件
- `qwen_api_audit.md` — Qwen API 官方核验
- `langsmith_audit.md` — LangSmith 免费额度核验
- `deepresearch_bench_ii.md` — DeepResearch Bench II 完整核验
- 本文件 `audit_summary.md` — 审计总结
