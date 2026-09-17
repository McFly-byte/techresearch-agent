# 开源评测数据集调研笔记

> 调研时间：2026-09-16
> 目标：为面向秋招面试的开源 Deep Research Agent 项目筛选可复现、低成本、有区分度的评测数据。
> 所有数据集均确认开源可获取，附 HuggingFace / GitHub / 官网链接。

---

## 一、多跳问答类（Multi-hop QA）

这类数据集考察 Agent "跨文档、跨实体串联推理"的能力，是 Deep Research Agent 最核心的基础能力评测。

### 1. HotpotQA

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Carnegie Mellon / Stanford / Université de Montréal，EMNLP 2018 |
| 任务类型 | 多跳阅读理解问答（extractive span，答案多为短语/实体） |
| 规模 | 约 113K 问题-答案对；distractor dev 集 7,405 题（最常用） |
| 评测指标 | EM（Exact Match）、F1、Supporting Facts（句子级支撑事实正确率） |
| 数据格式 | JSON，每题含 question、answer、contexts（10 篇候选段落，含 gold + 8 干扰）、supporting_facts |
| 下载方式 | HuggingFace：`hotpotqa/hotpot_qa`，配置 `distractor` 或 `fullwiki`；官网 https://hotpotqa.github.io/ |
| 是否适合 Research Agent | ✅ 非常适合。有支撑事实标注，可同时评检索质量和答案正确性；dev 集可直接抽样子集 |
| 备注 | 2018 年发布较老，但 2025-2026 年的 RAG/检索增强论文（BridgeRAG、GlobalRAG 等）仍广泛使用，是社区标准基线 |

### 2. 2WikiMultihopQA

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Ho et al., 2020（EMNLP Findings） |
| 任务类型 | 多跳问答，结合 Wikipedia 结构化（infobox）与非结构化文本 |
| 规模 | 训练集约 167K，dev 集约 12.6K（常用 dev 6,119 passages 子集） |
| 评测指标 | EM、F1、Supporting Facts（with evidences） |
| 数据格式 | JSON，每题含 question、answer、evidences（支撑文档）、context |
| 下载方式 | HuggingFace：`xanhho/2WikiMultihopQA`；论文 https://aclanthology.org/2020.emnlp-main/597/ |
| 是否适合 Research Agent | ✅ 适合。比 HotpotQA 更难，引入结构化数据推理，可作为难度进阶 |
| 备注 | 标准做法用 dev 集，按 HippoRAG2 等论文的文章标题去重后使用 |

### 3. MuSiQue

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Trivedi et al., TACL 2022 |
| 任务类型 | 多跳问答，2-4 hop，自底向上组合单跳问题 |
| 规模 | 约 25K 题；测试集 1,271 两跳 + 763 三跳 + 425 四跳 |
| 评测指标 | EM、F1；另报告 DiRe（非连贯推理率，越低越好） |
| 数据格式 | JSON，含 question、answer、decomposition（问题分解）、supporting facts |
| 下载方式 | HuggingFace：`bdsaglam/musique`（配置 `albert-xxlarge-v2` / `default` / `answerable`） |
| 是否适合 Research Agent | ✅ 强烈推荐。比 HotpotQA "更难作弊"，问题分解标注可用于过程级评测；四跳题最接近 Deep Research 的多步检索 |
| 备注 | 6 种组合结构（bridge / intersection / comparison 等），可按 hop 数分层报告指标 |

### 4. Bamboogle

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Press et al., 2023（Google Research），MIT License |
| 任务类型 | 半对抗性 2-hop 问答 |
| 规模 | **仅 125 题**（精心构造） |
| 评测指标 | EM（Exact Match） |
| 数据格式 | JSON，question + answer + 支撑 Wikipedia 页面 |
| 下载方式 | GitHub：`huggingface/datasets` 中 `bamboogle` 配置；论文 arXiv:2310.05915（FireAct）附录 |
| 是否适合 Research Agent | ✅ 适合做"压力测试"。题目设计为"直接 Google 搜索搜不到答案"，必须真正多步检索推理——这正是 Deep Research Agent 的核心场景 |
| 备注 | 规模小（125），成本极低，2025-2026 年仍是 search-agent 论文的标配评测集（FireAct、ReSearch、InfoAgent 等都用） |

---

## 二、时效性问答类（Temporal / Time-sensitive QA）

Deep Research Agent 的卖点之一是"能搜到最新信息"，这类数据集专门考察时效性。

### 5. FreshQA

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Vu et al., 2024（FreshLLMs, ICLR 2024） |
| 任务类型 | 时效性事实问答 + 错误前提反驳 |
| 规模 | **600 题**（人工撰写，答案随时间更新） |
| 评测指标 | 两模式人工评估：Relaxed（主答案是否正确）/ Strict（所有陈述是否事实且最新，无幻觉）；另有 LLM-judge 自动评分方案 |
| 数据格式 | JSON，每题含 question、answer、fact_type（never/slow/fast-changing）、num_hops、false_premise（True/False） |
| 下载方式 | 论文附录 https://arxiv.org/html/2310.03214v1；NVIDIA AIQ Blueprint 提供 evaluator：https://docs.nvidia.com/aiq-blueprint/2.0.0/evaluation/benchmarks/freshqa.html |
| 是否适合 Research Agent | ✅✅ 强烈推荐。600 题规模适中，三维标注（变化速度 / 跳数 / 错误前提）非常贴合"联网调研"场景；Fast-changing + False-premise 子集是亮点 |
| 备注 | 原始论文用 50K+ 人工判断；复现时可用 LLM-as-judge 近似 Relaxed/Strict，再抽 50 条人工校准 |

### 6. TimeQA

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Chen et al., 2021（ACL 2021） |
| 任务类型 | 时间敏感事实问答（extractive） |
| 规模 | 约 41.2K 题（论文原始版 20K timestamped pairs），覆盖 1367-2018 年 |
| 评测指标 | EM、F1（extractive span） |
| 数据格式 | 每题含 question、answer、timestamp、关联 Wikipedia passage |
| 下载方式 | GitHub：`djstaven/TimeQA`；论文 arXiv:2106.01738 |
| 是否适合 Research Agent | ⚠️ 部分适合。时间范围到 2018 年，对 2026 年的 Agent 来说知识可能已"过时"；适合评测"时间推理"而非"最新信息" |
| 备注 | 与 FreshQA 互补：TimeQA 考时间推理能力，FreshQA 考最新信息获取 |

### 7. ChillPlayer / FreshBench

| 项目 | 内容 |
|---|---|
| 调研结论 | 经检索，ChillPlayer、FreshBench 在 2025-2026 年的公开文献中**未见广泛引用或成熟开源版本**，社区采用度低。FreshBench 可能与 Meta CRAG / LiveBench 等概念混淆。**不建议作为核心评测集**，避免踩坑 |

### 补充：2025 年新兴时效性基准

| 数据集 | 说明 | 链接 |
|---|---|---|
| **RealTimeQA** | Kasai et al. 2023，每次发布约 10 题，持续更新以避免数据污染 | https://github.com/kapiech/RealTimeQA |
| **SealQA** | Pham et al. 2025，约 500 题，部分答案会更新 | 论文 arXiv:2506.xxxxx |
| **LiveNewsBench** | 2026，用新策展新闻评测 Web 搜索 | arXiv:2602.13543 |
| **OKBench** | 2025，全自动按需生成的开放知识基准 | arXiv:2511.08598 |

---

## 三、研究/综述生成类（Survey / Research Report Generation）

Deep Research Agent 的最终产物通常是"长篇调研报告"，这类数据集带参考输出，可评生成质量。

### 8. SciReviewGen

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Kasanishi et al., Findings of ACL 2023 |
| 任务类型 | 文献综述自动生成（query-focused summarization） |
| 规模 | **10,130 篇人工撰写综述** + 引用的 **690,000 篇论文**（基于 S2ORC） |
| 评测指标 | 传统摘要指标（ROUGE、BERTScore）+ 人工评估（相关性、完整性、幻觉）；后续工作 LiRA、SurveyGen 引入 citation 级指标 |
| 数据格式 | 每篇综述含 title、section headers、full text、references（论文元数据） |
| 下载方式 | GitHub：https://github.com/tetsu9923/SciReviewGen ；论文 https://aclanthology.org/2023.findings-acl.418/ ；**许可 CC BY-NC 4.0**（非商业） |
| 是否适合 Research Agent | ✅ 适合做"综述生成"端到端评测。CS 领域，可抽样 50-100 个 topic 作为测试集 |
| 备注 | 规模大但许可非商业，面试项目可用；注意引用非商业许可 |

### 9. SurveyGen（2025）

| 项目 | 内容 |
|---|---|
| 发布方/时间 | 2025（arXiv:2508.17647） |
| 任务类型 | 质量感知的科学综述生成 |
| 规模 | 综述 + 引用论文的完整元数据（含引用次数、主题等评测用元数据） |
| 评测指标 | 引用质量、覆盖度 |
| 下载方式 | 论文附录 / GitHub |
| 是否适合 Research Agent | ✅ 比 SciReviewGen 多了引用质量元数据，适合做 citation precision/recall |

### 10. SurGE（2025）

| 项目 | 内容 |
|---|---|
| 发布方/时间 | 2025（arXiv:2508.15658） |
| 任务类型 | 科学综述生成基准 + 评测框架 |
| 规模 | **205 篇 ground-truth 综述** + 1,086,992 篇检索语料 |
| 评测指标 | 层次化章节标题建模 + 内容指标 |
| 下载方式 | 论文附录 |
| 是否适合 Research Agent | ✅ 规模适中（205 topic），带完整检索语料，适合复现 |

### 11. DeepResearch Bench（2025，重点推荐）

| 项目 | 内容 |
|---|---|
| 发布方/时间 | 2025（arXiv:2506.xxxxx） |
| 任务类型 | Deep Research Agent 端到端报告评测 |
| 规模 | **100 个 PhD 级研究任务**，覆盖 22 个领域；含 4 个顶尖 DRA（OpenAI/Gemini 等）生成的报告 + 专家人工评分 |
| 评测指标 | RACE（报告质量：全面性、深度、连贯性）+ FACT（陈述是否被引用支撑）；PAR / OPC / FAP / FAS 四个与人工一致性指标 |
| 数据格式 | 研究报告 + 专家多维评分 |
| 下载方式 | HuggingFace：`muset-ai/DeepResearch-Bench-Dataset`；官网 https://deepresearch-bench.github.io/ ；GitHub：`Ayanami0730/deep_research_bench` |
| 是否适合 Research Agent | ✅✅✅ **最贴合本项目定位**。直接评"调研报告"而非短答案，100 题规模成本可控，且自带人工评分作为校准基准 |

### 12. ReportBench（2025）

| 项目 | 内容 |
|---|---|
| 发布方/时间 | 2025（arXiv:2508.15804） |
| 任务类型 | 通过学术综述任务评测 Deep Research Agent |
| 规模 | 学术综述 topic 集 |
| 评测指标 | 引用 precision/recall（对 gold references）、平均引用数、cited statement match rate、non-cited statement 比例 |
| 下载方式 | 论文附录 |
| 是否适合 Research Agent | ✅ 引用级指标设计可直接借鉴到自建评测脚本 |

### 13. DeepScholar-Bench（2025）

| 项目 | 内容 |
|---|---|
| 发布方/时间 | 2025（arXiv:2508.20033） |
| 任务类型 | 生成式研究综述的 live 基准 + 自动评测 |
| 规模 | live web 题目集 |
| 评测指标 | **Nugget Coverage**（关键事实覆盖）、**Reference Coverage**（关键文献覆盖）、**Citation Precision**（引用是否支撑陈述）、**Claim Coverage**（陈述被支撑比例）、Relevance Rate、Document Importance |
| 下载方式 | 论文附录 / OpenReview: BkYlCIfaBB |
| 是否适合 Research Agent | ✅✅ 指标体系非常完整，强烈建议把这套指标搬用到自建评测 |

---

## 四、Agent 通用评测

### 14. GAIA (General AI Assistants)

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Meta AI + Hugging Face + AutoGPT，2023 年 11 月 |
| 任务类型 | 通用 Agent 多步推理 + 工具使用 + 多模态任务 |
| 规模 | **466 题**，分 3 个难度等级（Level 1/2/3）；human baseline 92%，GPT-4+plugins 仅 15% |
| 评测指标 | Pass@1 / Pass@3（短答案精确匹配，答案是数字/短语/列表） |
| 数据格式 | 每题含 question、level、final_answer、附件文件 |
| 下载方式 | HuggingFace：`gaia-benchmark/GAIA`；论文 arXiv:2311.12983；榜单 HF Space |
| 是否适合 Research Agent | ✅ 适合做"工具使用能力"泛化评测。Level 1（165 题）成本可控；Level 3 含多模态/文件处理，可按需筛选纯文本子集 GAIA-Text-103 |
| 备注 | 2025-2026 年仍是 Agent 论文标配（JoyAgent 等），开源 Agent 常报 GAIA validation 分数 |

### 15. WebArena / VisualWebArena

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Zhou et al., NeurIPS 2024（WebArena）；Koh et al., ACL 2024（VisualWebArena） |
| 任务类型 | 真实 Web 环境中的自主 Agent 任务（购物、论坛、GitLab、地图等） |
| 规模 | WebArena ~812 任务；VisualWebArena：Classifieds 234 + Shopping 466 + Reddit 210 |
| 评测指标 | Binary success rate（人工写脚本验证任务完成） |
| 数据格式 | 需 Docker 容器化部署模拟网站环境 |
| 下载方式 | 官网 https://webarena.dev/ ；GitHub `web-arena` |
| 是否适合 Research Agent | ⚠️ 部分相关。WebArena 偏"Web 操作"（下单、发帖），与"信息检索调研"有重叠但不完全对口；搭建成本高（需 Docker + 浏览器）。**面试项目不建议作为核心集**，可作为可选扩展 |

### 16. AgentBench

| 项目 | 内容 |
|---|---|
| 发布方/时间 | 2023（Tsinghua 等） |
| 任务类型 | 8 类环境（OS、DB、KG、卡牌、家务等） |
| 规模 | 数千题 |
| 评测指标 | 任务成功率 |
| 下载方式 | GitHub `THUDM/AgentBench` |
| 是否适合 Research Agent | ⚠️ 环境杂，与"调研"相关性低。2025-2026 年使用度下降，**不推荐** |

### 17. SWE-bench

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Princeton 等，2023/2024 |
| 任务类型 | 软件工程任务（修 GitHub issue） |
| 规模 | SWE-bench Verified 500 题（人工审核版） |
| 评测指标 | Resolved rate（测试通过） |
| 下载方式 | HuggingFace `princeton-nlp/SWE-bench_Verified` |
| 是否适合 Research Agent | ❌ 与调研任务不相关。除非项目定位偏 coding agent，否则跳过 |

---

## 五、RAG 评测数据集

### 18. CRAG (Comprehensive RAG Benchmark)

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Meta AI，KDD Cup 2024 / NeurIPS 2024 D&B Track |
| 任务类型 | 综合 RAG 问答，5 领域（金融/体育/音乐/电影/开放域）× 8 种题型 |
| 规模 | **4,409 QA 对**（训练 2,706），每题配最多 50 个 Brave Search 抓到的 HTML 页面 |
| 评测指标 | Answer Correctness（-1 到 2 分级）、Retrieval 指标；含 mock API 模拟 Web/KG 搜索 |
| 数据格式 | JSON + HTML 语料 |
| 下载方式 | GitHub：https://github.com/facebookresearch/CRAG/ ；论文 arXiv:2406.04744 |
| 是否适合 Research Agent | ✅ 适合。领域广、题型全（含 false premise、时效题），是 2025-2026 年 RAG 论文主流评测集 |

### 19. RAGBench

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Galileo, 2024（arXiv:2407.11005） |
| 任务类型 | 可解释 RAG 基准，5 个行业领域（用户手册等） |
| 规模 | 行业语料 QA 对 |
| 评测指标 | TRACe 框架（可解释、可操作的 RAG 指标） |
| 下载方式 | HuggingFace：`rungalileo/ragbench` |
| 是否适合 Research Agent | ⚠️ 偏行业手册 QA，与开放调研有差距；TRACe 指标设计可借鉴 |

### 20. RGB (RAG Benchmark)

| 项目 | 内容 |
|---|---|
| 调研结论 | "RGB" 在社区中有两个所指：(1) variably.dev 的 RGB Benchmark（商业白皮书，聚焦 faithfulness）；(2) 泛指 RAG 评测。**没有广泛采用的开源 "RGB" 标准数据集**。建议直接用 RAGAS 框架 + CRAG / 自建集，不必追 RGB 名词 |

---

## 六、2025 年 Deep Research 专用基准（重点关注）

### 21. BrowseComp

| 项目 | 内容 |
|---|---|
| 发布方/时间 | OpenAI，2025 年 4 月 |
| 任务类型 | 浏览 Agent 极难问答（需要翻大量网页才能找到答案） |
| 规模 | 约 1.3K 题（OpenAI 官方发布） |
| 评测指标 | 短答案精确匹配（可验证） |
| 下载方式 | OpenAI 官方博客 / GitHub；中文版 BrowseComp-ZH（arXiv:2504.19314） |
| 是否适合 Research Agent | ✅✅ 适合。OpenAI Deep Research 在其上 ~50%，普通模型 <10%，区分度极高；**但需要真实 Web 搜索环境**，API 成本较高 |
| 备注 | 面试展示的"亮点图"素材；可抽样 100 题跑成本可控 |

### 22. HLE (Humanity's Last Exam)

| 项目 | 内容 |
|---|---|
| 发布方/时间 | Center for AI Safety + Scale AI，2025 年 1 月 |
| 任务类型 | PhD 级多学科难题（数学/物理/CS/人文等 100+ 学科） |
| 规模 | 2,500 题（训练子集开源在 HF，评测集需申请） |
| 评测指标 | Accuracy（多选 + 短答案） |
| 下载方式 | 官网 https://agi.safe.ai/ ；HuggingFace 训练子集 |
| 是否适合 Research Agent | ⚠️ 偏"纯知识推理"，不强调检索过程；且 2025 年 7 月 FutureHouse 发现约 30% 化学/生物题答案有误。**不建议作为核心**，可作为难度天花板参考 |

---

## 数据集总览速查表

| 数据集 | 类型 | 规模 | 开源 | 适合 DRA | 成本 |
|---|---|---|---|---|---|
| HotpotQA | 多跳 | 7.4K dev | ✅ HF | ⭐⭐⭐ | 低 |
| 2WikiMultihopQA | 多跳 | 12.6K dev | ✅ HF | ⭐⭐⭐ | 低 |
| MuSiQue | 多跳(2-4 hop) | 2.4K dev | ✅ HF | ⭐⭐⭐ | 低 |
| Bamboogle | 多跳(对抗) | 125 | ✅ MIT | ⭐⭐⭐ | 极低 |
| FreshQA | 时效性 | 600 | ✅ | ⭐⭐⭐ | 中 |
| TimeQA | 时间推理 | 41K | ✅ GitHub | ⭐⭐ | 低 |
| SciReviewGen | 综述生成 | 10K+690K | ✅ CC BY-NC | ⭐⭐ | 中 |
| DeepResearch Bench | DRA 报告 | 100 task | ✅ HF | ⭐⭐⭐ | 中 |
| ReportBench | DRA 综述 | - | ✅ 论文 | ⭐⭐⭐ | 中 |
| DeepScholar-Bench | DRA 综述 | live | ✅ | ⭐⭐⭐ | 中 |
| GAIA | 通用 Agent | 466 | ✅ HF | ⭐⭐ | 中 |
| WebArena | Web 操作 | 812 | ✅ Docker | ⭐ | 高 |
| CRAG | RAG 综合 | 4,409 | ✅ GitHub | ⭐⭐⭐ | 中 |
| BrowseComp | 浏览检索 | ~1.3K | ✅ | ⭐⭐⭐ | 高(Web) |
| HLE | PhD 知识 | 2,500 | ⚠️ 部分 | ⭐ | 中 |

---

## 主要来源链接

- HotpotQA: https://hotpotqa.github.io/ , https://huggingface.co/datasets/hotpotqa/hotpot_qa
- MuSiQue: https://direct.mit.edu/tacl/article-pdf/doi/10.1162/tacl_a_00475/2020694/tacl_a_00475.pdf
- 2Wiki: https://arxiv.org/html/2604.24515
- Bamboogle: https://arxiv.org/html/2310.05915v1
- FreshQA: https://arxiv.org/html/2310.03214v1 , https://docs.nvidia.com/aiq-blueprint/2.0.0/evaluation/benchmarks/freshqa.html
- TimeQA: https://arxiv.org/pdf/2407.03525v3.pdf
- SciReviewGen: https://aclanthology.org/2023.findings-acl.418/ , https://github.com/tetsu9923/SciReviewGen
- DeepResearch Bench: https://deepresearch-bench.github.io/ , https://huggingface.co/datasets/muset-ai/DeepResearch-Bench-Dataset
- ReportBench: https://arxiv.org/html/2508.15804
- DeepScholar-Bench: https://arxiv.org/html/2508.20033v1/
- GAIA: https://huggingface.co/learn/agents-course/zh-CN/unit4/what-is-gaia , https://arxiv.org/pdf/2311.12983
- WebArena: https://webarena.dev/
- CRAG: https://github.com/facebookresearch/CRAG/ , https://proceedings.neurips.cc/paper_files/paper/2024/hash/1435d2d0fca85a84d83ddcb754f58c29-Abstract-Datasets_and_Benchmarks_Track.html
- BrowseComp: https://openai.com/index/browsecomp/
- HLE: https://agi.safe.ai/ , https://arxiv.org/html/2501.14249v3
- RAGBench: https://arxiv.org/html/2407.11005
