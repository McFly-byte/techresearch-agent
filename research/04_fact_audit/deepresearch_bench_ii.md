# DeepResearch Bench II 深度核验报告

> 核验日期：**2026-09-16**
> 核验来源：官方项目页 + arXiv 论文摘要/正文（v2）原文。
> 证据等级：✅ 官方/论文确认｜ 🟡 二手来源｜ ⚪ 待确认（GitHub 被 robots.txt 拦截，未能直读）

---

## 0. 一句话结论

DeepResearch Bench II 是一个**真实存在、已公开论文、已开源代码与 rubrics** 的“深度研究 Agent”评测集：132 个研究任务、9,430 条专家撰写的二元评分细则（rubric），用 **LLM 当裁判**逐条判“过/不过”。它**非常适合**评估“多步联网检索 → 综合 → 长报告”这类能力，和你要做的“技术方案调研 Agent”高度相关；但它**不是工程/代码类 benchmark**，且许可证为**非商用（CC BY-NC-SA 4.0）**。

---

## 1. 基本信息（✅ 官方/论文确认）

| 项目 | 内容 |
|---|---|
| 全称 | **DeepResearch Bench II: Diagnosing Deep Research Agents via Rubrics from Expert Reports** |
| 作者单位 | 中国科学技术大学（USTC）+ Metastone Technology（北京） |
| 通讯/lead | Benfeng Xu（lead）、Zhendong Mao（通讯）；共同一作 Ruizhe Li、Mingxuan Du |
| 发布状态 | **PREPRINT 2026** |
| arXiv 编号 | **arXiv:2601.08536**（v1 提交 2026-01-13，v2 修订 2026-01-30） |
| DOI | https://doi.org/10.48550/arXiv.2601.08536 |
| **许可证** | **CC BY-NC-SA 4.0（知识共享，-NC = 非商用）** ⚠️ |
| 项目主页 | https://agentresearchlab.com/benchmarks/deepresearch-bench-ii/index.html |
| **代码仓库** | **https://github.com/imlrz/DeepResearch-Bench-II**（论文原文明确：“We release the benchmark, evaluation scripts, and all rubrics at …”） |

---

## 2. 数据集规模与构成（✅ 论文/官网确认）

| 指标 | 数值 |
|---|---|
| 研究任务数 | **132 个 grounded（有据可依）研究任务** |
| 领域数 | **22 个 topic domains** |
| 评分细则（rubric）总数 | **9,430 条 fine-grained binary rubrics** |
| 每个任务平均 rubric 数 | InfoRecall **52.9** / Analysis **12.8** / Presentation **5.7** |
| 人工投入 | **400+ 人工小时**（论文正文；官网首页写 300+，以论文 400+ 为准） |
| rubric 来源 | 从**专家撰写的深度调查报道**中提取，经“LLM 抽取→自评过滤→人工清洗→领域专家精修”**四阶段流程** |

任务示例（官网截图）：
- “Where is Donald Trump? Why his policy is so AGGRESSIVE?”
- “WHAT'S next for Elon Musk?”

任务是**开放式深度调查/分析报告**，不是固定答案的问答。

---

## 3. 三维评测框架与指标（✅ 论文/官网确认）

| 维度 | 评什么 |
|---|---|
| **Information Recall（信息召回）** | 能否准确、全面地从网上检索到该找的证据（知道该找什么、找得到、并溯源验证） |
| **Analysis（分析）** | 能否综合多源信息、提炼出超越原始数据的**新洞见**（趋势、范式、对立观点权衡） |
| **Presentation（呈现）** | 报告是否可信、可验证、结构清晰（引用、表格、图表、面向读者的认知水平） |
| **TotalScore** | 上述三维加权总分（官方说明“重内容质量、轻排版”） |

**评分方式（关键）**：
- 每条 rubric 是**二元（pass/fail）**、原子化、可验证的具体陈述（例：“明确指出巴基斯坦 Benazir Income Support Programme 未覆盖最贫困五分位人群的 79%”）。
- **用 LLM 当裁判（LLM-as-judge）端到端判断每条 rubric 是否满足**，输出各维度通过率（%）。
- 论文额外做了 **human–LLM agreement（人机一致性）研究**来验证裁判可靠性。
- 因此它**不是纯人工评分**，也**不是简单 LLM 打分**——而是“专家定细则 + LLM 逐条判定 + 人工一致性校验”。

**排行榜现状（官网，2026）**：前 3 名为 AI21-DeepResearch（64.38）、Dalpha DeepResearch（61.01）、WhaleCloud-DocChain（60.94）；OpenAI o3 Deep Research 45.40、Gemini-3-Pro DR 44.60、Doubao DR 40.99、Qwen3-Max DR 39.25。**最强系统也只通过不到 50% 的 rubrics**（InfoRecall/Analysis 尤其弱）。

---

## 4. 代码仓库与数据集可获取性

| 项 | 结论 | 证据等级 |
|---|---|---|
| 是否开源 | ✅ 论文承诺发布 benchmark + 评测脚本 + 全部 rubrics | ✅ 论文 |
| 仓库地址 | https://github.com/imlrz/DeepResearch-Bench-II | ✅ 论文原文链接 |
| License（仓库级） | 论文整体 CC BY-NC-SA 4.0（沿用原始专家文章的 CC BY / CC BY-NC 授权，**仅限非商用**） | ✅ 论文第 8 节 |
| 数据集是否公开下载 | ✅ 论文称“release … all rubrics”，预期在 GitHub 仓库中公开 | ✅（论文）/ ⚪（仓库内容未直读） |
| 数据格式（JSON/JSONL） | ⚪ **待确认**——本次 GitHub 直读被 robots.txt 拒绝，未能核实文件后缀与目录结构 | ⚪ 待确认 |
| star 数 / 最后更新 | ⚪ **待确认**（同上原因，未直读 GitHub） | ⚪ 待确认 |
| 标准答案（ground truth） | 以“专家报告 + 9,430 条 rubric”作为可验证依据（rubric 即真值点），而非单一标准答案 | ✅ 论文 |

> **诚实说明**：github.com 对本工具返回 `robots.txt disallowed`，我**没有绕过**。仓库 URL 与“已发布 rubrics/脚本”由 arXiv 论文原文确认无误；但 star 数、最后提交时间、确切文件格式/目录结构，请你在浏览器打开 https://github.com/imlrz/DeepResearch-Bench-II 自行核对 README（这是唯一未闭环的一环）。

> **配套说明**：注意区分它和第一代 **DeepResearch Bench I**（arXiv:2506.11763，100 个 PhD 级任务、22 领域、RACE/FACT 评测，仓库 `github.com/Ayanami0730/deep_research_bench`）。II 是其升级版，rubrics 更细、可验证性更强。

---

## 5. 评测方法如何复现（✅ 论文 + ⚪ 部分待确认）

1. 让被测 Agent 在**联网**环境下完成 132 个任务中的若干个，产出长报告。
2. 用论文发布的 rubrics 与评测脚本，调一个 LLM judge 逐条判定 pass/fail。
3. 汇总各维度通过率（%）与加权 TotalScore。
4. **论文自己提示的风险与复现注意事项**：
   - **数据泄漏风险**：任务源自公开专家文章，Agent 可能直接搜到原文而“作弊”。论文建议**在搜索工具层屏蔽这些源文章**，否则分数虚高。
   - **人工标注偏差**：专家报告也未必让所有评审满意，评测本身是“长尾难题”。
   - **Presentation 维度局限**：目前只评格式排版，尚未按用户背景个性化呈现。
5. **成本**：需要联网 Agent 运行成本 + LLM judge 调用成本（9,430 条 rubric 逐条判，judge token 消耗不小，建议先在子集上试跑）。

---

## 6. 是否适合“技术方案调研”类 Agent？（评估）

### 结论：**高度适合作为“研究报告质量”的主评测，但不是纯工程/代码 benchmark。**

**适合的理由 ✅**：
- 你要做的“技术选型 / 架构决策 / 可行性报告”，本质就是 **多步联网检索 → 综合 → 结构化长报告**——这正是 Bench II 测的三件事（召回、分析、呈现）。
- rubric 是**原子、可验证**的（“是否提到了 X 数据点”“是否对比了正反观点”），比“让 LLM 主观打分”更可靠，适合做**回归基线**。
- 覆盖 22 个领域，任务开放、贴近真实调研场景。

**不完美 / 局限 ⚠️**：
- 任务偏“调查报道/分析师报告”，**不涉及写代码、跑测试、读 GitHub 仓库**——纯技术选型里的“可行性验证（跑通 demo）”测不到。
- 需要**联网环境**与**LLM judge 成本**，新人复现门槛不低。
- **许可证 CC BY-NC-SA 4.0，禁止商用**——个人研究/毕业设计 OK，公司产品化评测需注意授权。
- 评测偏“结果质量”，**不直接测 Agent 的工具调用轨迹正确率/中间过程**。

---

## 7. 替代 / 补充 benchmark 推荐（若要更贴合技术调研）

1. **DeepResearch Bench II 本身作为主评测（推荐保留）** —— 最贴合“联网调研→长报告”闭环，rubric 可验证。先用它跑 132 任务（或其子集）拿基线。
2. **补充：BrowseComp / GAIA（工具使用与检索 grounding）** —— 若想专门测“Agent 会不会用搜索工具、定位到正确来源”，这类更聚焦检索/工具调用的 benchmark 是好补充。
3. **若偏工程实现可行性：SWE-bench Verified** —— 若你的“技术方案调研”最终要落到“能否真的把方案写出来/修出来”，SWE-bench 类软件工程评测可作补充（但它是代码任务，不是调研任务）。

> 一句话选型：**调研“报告质量”用 DeepResearch Bench II；调研“工具检索准确度”补 BrowseComp/GAIA；调研“代码实现”才上 SWE-bench。**

---

## 对技术设计的影响（给 Agent 开发者）
1. 把 Bench II 作为**离线回归评测集**：每次改 Agent prompt/工具后，在固定子集上重跑，对比三维通过率。
2. 复现时务必**屏蔽来源专家文章**防泄漏，并预算 LLM judge 成本。
3. 因 **CC BY-NC-SA**，仅限非商用研究；商用产品评测需评估授权。
4. 仓库文件格式/stars 等未直读项，请你浏览器打开 GitHub README 补齐。

---

## 官方 URL 清单
- 项目主页/排行榜：https://agentresearchlab.com/benchmarks/deepresearch-bench-ii/index.html
- arXiv 摘要：https://arxiv.org/abs/2601.08536
- arXiv 正文 HTML：https://arxiv.org/html/2601.08536v2
- 代码与数据仓库：https://github.com/imlrz/DeepResearch-Bench-II
- 前作 DeepResearch Bench I：https://arxiv.org/abs/2506.11763 ；https://deepresearch-bench.github.io/
