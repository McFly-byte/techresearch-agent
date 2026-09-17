# 评测工具与框架对比

> 调研时间：2026-09-16
> 目标：对比 2025-2026 年主流 LLM/RAG/Agent 评测框架，为本项目选 1 个主力工具。

---

## 一、五强对比总表

| 框架 | 开源 | 主接口 | 最佳场景 | 学习曲线 | 自定义指标 |
|---|---|---|---|---|---|
| **RAGAS** | Apache 2.0 | Python 库 | RAG 专项评测 | 低 | ✅ 中等 |
| **DeepEval** | Apache 2.0 | Python + Pytest + CLI | Agent/RAG/聊天机器人全栈评测 + CI/CD | 低 | ✅✅ 强 |
| **TruLens** | MIT | Python 装饰器 | RAG 可观测 + 在线监控 | 中 | ✅ 中 |
| **LangSmith** | 闭源 | UI + SDK | LangChain 生态原生 | 中 | ✅ 中 |
| **Phoenix (Arize)** | Elastic License 2.0 | Python/TS + UI + OTel | 可观测驱动评测 | 中 | ✅ 中 |

---

## 二、RAGAS

### 定位
**RAG 评测的事实标准**，学术论文引用最多。专为 RAG 场景设计，不适合 Agent 轨迹评测。

### 核心指标（开箱即用）
- **Faithfulness**：回答是否忠实于检索上下文
- **Context Precision**：检索到的上下文里有多少是真正相关的
- **Context Recall**：gold answer 里的信息有多少被检索到了
- **Response Relevancy**：回答是否切题

### 优点
- 论文背书（arXiv:2312.10997），指标定义严谨
- Python API 极简：`evaluate(dataset, metrics=[...])` 一行搞定
- 支持自定义 LLM judge（可接 DashScope/Qwen）
- 与 LangChain 深度集成

### 缺点
- **只管 RAG，不管 Agent 过程**（多步检索轨迹、工具调用）
- 无生产监控、无 UI 看板
- 新版本（0.2+）API 变更频繁，老教程可能跑不通

### 上手复杂度：⭐⭐（很低）
```python
from ragas import evaluate
from ragas.metrics import faithfulness, context_precision, context_recall
result = evaluate(dataset=my_samples, metrics=[faithfulness, context_precision])
```

### 适合本项目吗？
✅ **适合做 RAG 模块指标**（faithfulness / context precision），但不够覆盖 Agent 端到端。

### 链接
- 官网：https://docs.ragas.io/
- GitHub：https://github.com/explodinggradients/ragas

---

## 三、DeepEval

### 定位
**Python 原生、Pytest 风格的 LLM 评测框架**，最像"写单元测试"，适合 CI/CD。

### 核心指标（14+ 内置）
- G-Eval（LLM-judge 通用评分）
- Faithfulness、Answer Relevancy、Context Precision
- Hallucination、Toxicity、Bias、PII Leakage（安全/红队）
- Conversational metrics（多轮对话）
- **CustomMetric**：可写任意 Python 函数当指标

### 优点
- **Pytest 风格**：写 `def test_...(self):` 就像写单测，Agent 开发新人最熟悉
- 内置 **Confident AI** 平台（可选手动看报告）
- 支持红队测试、安全扫描
- CI/CD 友好（`deepeval test run` 直接进 pipeline）
- 自定义指标最灵活

### 缺点
- 指标定义偏"工程化"，学术论文引用不如 RAGAS 多
- UI 报告要接 Confident AI（可不用，但体验打折）

### 上手复杂度：⭐⭐（很低）
```python
from deepeval import assert_test
from deepeval.metrics import HallucinationMetric, AnswerRelevancyMetric
from deepeval.test_case import LLMTestCase

def test_answer():
    metric = HallucinationMetric(threshold=0.5)
    test_case = LLMTestCase(input="...", actual_output="...", context=["..."])
    assert_test(test_case, [metric])
```

### 适合本项目吗？
✅✅ **强烈推荐作为主力评测框架**。Pytest 风格对新人友好，自定义指标（citation precision、nugget coverage）好写，CI 友好，面试展示时 `deepeval test run` 的输出很专业。

### 链接
- 官网：https://deepeval.com/
- GitHub：https://github.com/confident-ai/deepeval
- 对比文：https://deepeval.com/blog/top-5-llm-evaluation-frameworks

---

## 四、TruLens

### 定位
**RAG 可观测 + 评测一体**，核心概念是 "RAG Triad"。

### RAG Triad 三指标
1. **Context Relevance**：检索的上下文是否和问题相关
2. **Groundedness**：回答是否基于上下文（= faithfulness）
3. **Answer Relevance**：回答是否切题

### 优点
- 装饰器风格（`@tru` 标注函数），集成到现有代码几乎零侵入
- LangChain / LlamaIndex 原生集成
- 在线追踪（记录每次调用的 trace）
- OpenTelemetry 兼容

### 缺点
- 设计理念偏"在线监控"，离线批量评测不如 DeepEval 顺手
- 指标粒度不如 RAGAS 学术化
- 文档更新慢

### 上手复杂度：⭐⭐⭐（中等）

### 适合本项目吗？
⚠️ 可选。如果项目用了 LangChain，TruLens 集成顺；否则 DeepEval 更直接。

### 链接
- 官网：https://www.trulens.org/
- GitHub：https://github.com/truera/trulens

---

## 五、LangSmith

### 定位
**LangChain 官方的评测 + 可观测平台**，闭源 SaaS。

### 核心能力
- 数据集管理、离线评测、在线评测
- LLM-as-judge、pairwise 对比
- **轨迹评测器**（trajectory evaluators）：评 Agent 的工具调用路径——这是 Agent 评测的独特优势
- 人工标注队列（annotation queues）
- 多线程对话评测

### 优点
- 如果项目用 LangChain / LangGraph，**集成零成本**
- Trace 可视化做得最好，能回放 Agent 每一步
- 团队协作、标注队列成熟

### 缺点
- **闭源 SaaS**，数据要发到 LangChain 服务器（隐私顾虑）
- 脱离 LangChain 生态后体验打折
- 按用量收费，大规模评测成本高
- 不可自托管（2025 年仍无自部署版）

### 上手复杂度：⭐⭐⭐（中等）

### 适合本项目吗？
⚠️ 如果项目用 LangChain → 推荐用 LangSmith 看 trace；如果纯自己写的 Agent 框架 → 不必引入。面试项目展示 trace 回放图很加分，但要注意数据隐私。

### 链接
- 官网：https://www.langchain.com/langsmith
- 对比文：https://www.langchain.com/resources/langsmith-vs-arize

---

## 六、Phoenix (Arize)

### 定位
**可观测驱动的 LLM 评测**，基于 OpenTelemetry。

### 核心能力
- OTel 原生 trace 收集（任何框架都能接）
- 离线 + 在线评测
- LLM-as-judge、人工标注队列
- 内置 RAG 指标

### 优点
- **开源可自托管**（Elastic License 2.0）
- 对已有 ML 可观测团队无缝扩展
- Trace UI 强

### 缺点
- 偏"运维监控"思维，评测指标不如 DeepEval/RAGAS 专精
- Elastic License 2.0 不是 OSI 认可的开源（商用有限制）
- 文档偏企业用户

### 上手复杂度：⭐⭐⭐（中等）

### 适合本项目吗？
⚠️ 不推荐作为主力。面试项目用不上 OTel 那套重装备。

### 链接
- 官网：https://docs.arize.com/phoenix
- GitHub：https://github.com/Arize-ai/phoenix

---

## 七、其他值得知道的工具

| 工具 | 一句话 | 是否推荐 |
|---|---|---|
| **Promptfoo** | YAML 配置 + CLI，prompt 对比和安全测试强 | ⭐⭐ 可做 prompt 回归 |
| **Langfuse** | 开源可自托管的 trace + 轻量评测 | ⭐⭐ 想自托管 trace 时用 |
| **MLflow** | 传统 MLflow + 插件式接 DeepEval/RAGAS | ⭐ 已有 MLflow 项目才用 |
| **EvalScope**（阿里） | 阿里云出的评测框架，支持 GAIA 等 | ⭐⭐ 用 DashScope 时文档友好 |
| **ARES** | 斯坦福的 RAG 评测，需微调 judge，成本高 | ❌ 太重 |

---

## 八、本项目推荐选型

### 主力：DeepEval
- 理由：Pytest 风格新人友好、自定义指标强、CI 友好、输出专业
- 承担：端到端评测 + 自定义 citation/faithfulness 指标

### 补充：RAGAS
- 理由：学术背书，faithfulness/context precision 直接搬
- 承担：RAG 模块指标，和 DeepEval 交叉验证

### 可选：LangSmith trace
- 理由：面试展示 Agent 每一步思考过程的回放图
- 承担：可视化调试 + 失败案例分析

### 不引入
- TruLens / Phoenix / ARES：过重或不匹配
- LangSmith 深度集成：避免绑定 LangChain

---

## 主要来源链接

- DeepEval 框架对比：https://deepeval.com/blog/top-5-llm-evaluation-frameworks
- DeepEval vs Arize：https://deepeval.com/blog/deepeval-vs-arize
- RAGAS 文档：https://docs.ragas.io/en/v0.2.11/howtos/integrations/langchain/
- TruLens RAG Triad：https://arxiv.org/pdf/2401.17043v3.pdf
- LangSmith vs Arize：https://arize.com/compare/arize-vs-langsmith/
- MLflow Agent 评测对比：https://www.mlflow.org/top-5-agent-evaluation-frameworks/
- RAG 评估工具综述：https://blog.csdn.net/m0_63309778/article/details/152823514
- 指标数学原理：https://www.shyankdev.com/blogs/rag-evaluation-metrics-ragas-trulens
