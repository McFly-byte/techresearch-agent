# 任务 2：知识图谱集成方案 · 选型报告

> 项目：技术方案调研 DeepResearchAgent。核心场景：从 Web / 论文 / 本地 PDF·Markdown 调研结果中，抽取"技术实体—关系—对比"，支撑跨文档比较与架构决策。
> 调研日期：2026-09-16。Star 为 GitHub API 快照。

---

## 0. 先回答一个更重要的问题：MVP 阶段到底要不要知识图谱？

**结论：MVP 阶段不要上完整的知识图谱数据库。**

理由（针对本项目 3 个月单人、调研类 Agent）：
1. **知识图谱的价值是"跨文档多跳推理"**（A 的作者做了 B，B 用了 C…）。而 MVP 的核心痛点是"把 N 篇资料读懂、产出一份选型报告"——这是**长文档理解 + 结构化抽取**，不是图遍历。
2. **建图很贵**：图 RAG 每篇文档都要 LLM 抽实体/关系，token 成本高、索引慢；微软 GraphRAG 还要预跑社区报告。
3. **单人项目维护成本**：图库（Neo4j/Kuzu）+ schema 设计 + 查询调试，会吃掉本就紧张的 3 个月。
4. **MVP 的"知识图谱"其实可以很轻**：让 LLM 在产出报告时，顺手把结论落成一个 **结构化 JSON / CSV 表**（实体、属性、关系、对比项），前端直接渲染。这已经能解决 80% 的"对比"需求。

> 一句话：**MVP 用"向量 RAG + 结构化抽取成表"；进阶再上 LightRAG 做真图。**

---

## 1. 候选方案对比

### 1.1 LightRAG（HKUDS/LightRAG）★ 进阶首选

| 项 | 内容 |
|---|---|
| GitHub | https://github.com/HKUDS/LightRAG |
| 论文 | "LightRAG: Simple and Fast Retrieval-Augmented Generation"，EMNLP 2025，arXiv:2410.05779 |
| Star | 约 2.4 万（API 快照 24,119，fork 3,530；2026 年第三方报道约 3.8–4 万，增长快） |
| License | **MIT** ✅（最宽松，开源友好） |
| 包名 | `lightrag-hku`（PyPI） |
| 核心原理 | **双层检索（dual-level retrieval）**：<br>① 索引时把文档切块 → LLM 抽实体+关系建**增量知识图谱**；同时做向量索引<br>② 查询时分 global（实体/主题级，遍历相关子图）和 local（实体邻域）两路并行召回，合并后喂 LLM。与微软 GraphRAG 的关键差异：**插入时增量建图、查询时只走相关子图**，不预跑昂贵的社区报告 |
| 存储后端 | 内置：SQLite（图）+ FAISS/NanoVectorDB（向量）开箱即用；可换 Postgres、Neo4j、Memgraph、Qdrant、Milvus、MongoDB、OpenSearch、Redis |
| LangGraph 集成难度 | **低**。它是纯 Python 库，`insert()` / `query()` 两个 API，包成 LangGraph 的一个"检索工具节点"即可，无需改 LangGraph 调度 |
| 适用场景 | 文档量 10MB–1GB、需要跨文档关系问答、又不想扛微软 GraphRAG 的成本 |
| 优点 | ① MIT、轻量、可嵌入式（SQLite+FAISS 零服务）；② 增量更新，不用全量重索引；③ 比 GraphRAG 便宜一个数量级；④ 社区活跃、EMNLP 论文背书 |
| 缺点 | ① 实体抽取质量依赖所接 LLM；② schema 是自动生成的，想精细控制实体类型要调 prompt；③ 相对年轻，API 偶尔 breaking |

**最小用法**：
```python
# pip install lightrag-hku
from lightrag import Lightrag, QueryParam
rag = Lightrag(working_dir="./lr_storage")      # 默认 SQLite + FAISS，零服务
rag.insert("LightRAG 由港大 HKUDS 开发，主打双层检索，2025 年发表于 EMNLP。")
ans = rag.query("LightRAG 和微软 GraphRAG 的核心区别？",
                param=QueryParam(mode="hybrid")) # local + global 混合
```

---

### 1.2 Microsoft GraphRAG（microsoft/graphrag）

| 项 | 内容 |
|---|---|
| GitHub | https://github.com/microsoft/graphrag ；文档 https://microsoft.github.io/graphrag/ |
| Star | 约 3.6 万（API 快照 35,971，fork 3,788；2026-09 仍活跃） |
| License | **MIT** ✅ |
| 核心原理 | LLM 抽实体/关系/claim 建图 → **Leiden 社区检测**分层聚类 → 对每个社区预生成**摘要报告（community report）**；查询时：local search（实体邻域）或 global search（map-reduce 汇总各社区报告，回答"整个语料的主题是什么"） |
| 存储 | Parquet + LanceDB/向量；CLI `graphrag index` / `graphrag query` |
| LangGraph 集成难度 | **中**。它是独立 pipeline（CLI + Python API），可包成工具，但 indexing 是离线重活，不适合放进 LangGraph 的实时调度 |
| 适用场景 | 大规模语料、需要"全局主题报告"（例如"这 100 篇论文的整体趋势"） |
| 优点 | ① 微软背书、文档成熟；② **global search 的社区报告**是独门能力；③ MIT |
| 缺点 | ① **索引极贵**（实体抽取 + 全量社区报告，token 成本高）；② 增量更新弱，大改要重跑；③ 概念重（社区、claims、map-reduce）；④ 对单人 MVP 是杀鸡用牛刀 |

> 结论：能力标杆，但**成本和复杂度不匹配 MVP**。它和 LightRAG 的取舍本质是"预计算全图报告" vs "查询时走相关子图"——单人项目选后者。

---

### 1.3 Neo4j + LLM（传统图数据库）

| 项 | 内容 |
|---|---|
| GitHub（驱动/生态） | 官方库 https://github.com/neo4j/neo4j ；LangChain 集成 `langchain-neo4j` |
| 定位 | 成熟的商用图数据库，Cypher 查询语言事实标准 |
| 核心原理 | 自己用 LLM 从文档抽 (实体)-[关系]->(实体) 三元组写入 Neo4j，查询用 Cypher 或 LLM 生成 Cypher（`GraphCypherQAChain`） |
| 存储 | Neo4j 服务（需常驻，Desktop 版或 Neo4j Aura 云） |
| LangGraph 集成难度 | **中**：LangChain 有 `Neo4jGraph` / `GraphCypherQAChain`，包成工具即可；但要起 Neo4j 服务、设计 schema、维护 Cypher |
| 适用场景 | 需要多跳查询、团队已有 Neo4j、数据规模大 |
| 优点 | ① 最成熟、生态最好、Cypher 标准；② 可视化浏览器调试方便 |
| 缺点 | ① **要常驻服务**（Docker），本地开发重；② schema 要自己设计和维护；③ LLM 写 Cypher 容易出错；④ 对"自动从文档建图"只给零件不给流水线 |

> 结论：MVP 不上。进阶如果需要多跳 + 可视化，Neo4j 是稳妥选择，但 LightRAG 在"自动建图+自动检索"上更省事。

---

### 1.4 Kuzu（kuzudb/kuzu）

| 项 | 内容 |
|---|---|
| GitHub | https://github.com/kuzudb/kuzu ；文档 https://kuzudb.com/ |
| Star | 约 3.5 千（API 快照；C++ 实现） |
| License | **MIT** ✅ |
| 核心原理 | **嵌入式（in-process）属性图数据库**，无服务器，盘上/内存运行，支持 Cypher、全文检索、HNSW 向量索引 |
| 存储 | 本地文件目录，`python -m pip install kuzu` 后 `kuzu.Database("./db")` 即开即用 |
| LangGraph 集成难度 | **低**：LangChain 官方有 `langchain-kuzu`（KuzuGraph）；但它只给"图存取"，**不给"LLM 自动建图流水线"** |
| 适用场景 | 想要 Cypher 的表达力、又不想起 Neo4j 服务；本地/嵌入式分析 |
| 优点 | ① 零服务、零运维，最适合本地开发；② Cypher 兼容；③ MIT；④ 自带向量索引，可同时存图+向量 |
| 缺点 | ① 相对年轻、社区小；② 它是"图数据库零件"，建图/抽取/检索编排要自己写（或配 LightRAG/Cognee 当它的图后端） |

> 结论：**如果进阶阶段你要自己掌控 schema 又不想起 Neo4j，Kuzu 是最佳嵌入式底座**；但它不单独解决"从文档自动建图"。

---

## 2. 横向对比表

| 维度 | LightRAG | MS GraphRAG | Neo4j+LLM | Kuzu |
|---|---|---|---|---|
| GitHub | HKUDS/LightRAG | microsoft/graphrag | neo4j/neo4j | kuzudb/kuzu |
| Star | ~2.4 万 | ~3.6 万 | （图 DB 老牌） | ~3.5 千 |
| License | MIT | MIT | GPLv3/商用双协议 | MIT |
| 建图方式 | **自动、增量** | 自动、批量重索引 | 自己写抽取 | 自己写抽取 |
| 查询 | dual-level 自动 | local / global(map-reduce) | Cypher / LLM→Cypher | Cypher |
| 要不要常驻服务 | 否（SQLite+FAISS） | 否（离线 pipeline） | **是** | 否 |
| Token 成本 | 中 | **高**（预跑社区报告） | 低（只存取） | 低 |
| 上手难度 | 低 | 高 | 中 | 中 |
| 对 LangGraph | 包成工具即可 | 包成工具即可 | 包成工具即可 | 包成工具即可 |
| 适合阶段 | **进阶首选** | 大规模/全局报告 | 有图 DB 经验 | 进阶嵌入式底座 |

---

## 3. 推荐方案

### ✅ MVP：不上知识图谱，用"向量 RAG + 结构化抽取成表"

具体做法（与记忆层解耦）：
1. 文档切块 → 嵌入 → 存 **Chroma / LanceDB**（本地、零服务，和 LightRAG 默认后端同族）。
2. 调研报告产出时，让 LLM 额外输出一段 **结构化 JSON**：
   ```json
   {
     "entities": [{"name": "LightRAG", "type": "framework", "attrs": {"license": "MIT", "stars": 24000}}],
     "relations": [{"source": "LightRAG", "target": "HKUDS", "type": "developed_by"}],
     "comparisons": [{"item": "索引成本", "LightRAG": "低", "GraphRAG": "高"}]
   }
   ```
3. 前端拿这个 JSON 直接渲染对比表 / 关系图（可用 ECharts graph）。
4. 这些实体/关系同时写进 LangGraph Store（见任务 1），跨 run 复用。

> 这样你在**不引入图库**的前提下，已经拥有了"知识图谱"的最外层价值（结构化实体+关系+对比）。

### 🚀 进阶：引入 LightRAG 做真图 RAG

**触发条件**：MVP 跑通后，出现这些需求之一——
- 跨文档多跳问答变差（"X 的作者还做过哪些和 Y 有关的项目？"）；
- 资料库累积到几十篇以上、纯向量召回不够；
- 需要把历史调研报告沉淀成可复用的知识库。

**为什么是 LightRAG 而不是 GraphRAG/Neo4j**：
- MIT、纯库、默认 SQLite+FAISS **零服务**，最贴合单人开源项目；
- 增量建图，不用像 GraphRAG 那样动辄重跑全量索引；
- token 成本远低于 GraphRAG；包成 LangGraph 一个检索工具节点即可。

**升级路径**：
```
MVP：Chroma/LanceDB 向量检索 + LLM 抽 JSON 表
  ↓
进阶：LightRAG（SQLite+FAISS 后端）替代纯向量检索，保留 JSON 表做前端渲染
  ↓
（可选）重度：图后端换 Kuzu 或 Neo4j，需要多跳+Cypher/可视化时
```

---

## 4. 集成代码骨架（进阶：LightRAG 包成 LangGraph 工具节点）

```python
# kg_tool.py —— 把 LightRAG 检索包成 LangGraph 节点/工具
# 依赖：pip install lightrag-hku langgraph
from lightrag import Lightrag, QueryParam

_rag = None

def get_rag(working_dir: str = "./lr_storage") -> Lightrag:
    """懒加载单例：默认 SQLite(图)+FAISS(向量)，零外部服务。"""
    global _rag
    if _rag is None:
        _rag = Lightrag(working_dir=working_dir)
        # 真实项目里在这里配置 llm_model_func / embedding_func
    return _rag

def kg_index(text: str) -> str:
    """调研到新资料时调用：增量建图 + 向量。"""
    get_rag().insert(text)
    return "indexed"

def kg_search(question: str) -> str:
    """LangGraph 里的检索工具：hybrid = local(实体邻域) + global(主题子图)。"""
    return get_rag().query(question, param=QueryParam(mode="hybrid"))

# 在 LangGraph 里像这样当工具用（create_react_agent 或自研节点）：
# from langchain_core.tools import tool
# @tool
# def knowledge_search(q: str) -> str:
#     """检索已沉淀的技术知识图谱。"""
#     return kg_search(q)
```

> MVP 阶段你只需要 `kg_search` 对应的"向量检索 + JSON 抽取"两个函数，LightRAG 这层先注释掉、留接口。

---

## 5. 一句话决策

> **MVP：不上图数据库，向量 RAG + LLM 抽 JSON 实体/关系/对比表。**
> **进阶：上 LightRAG（MIT、零服务、增量建图、包成 LangGraph 工具节点）。**
> 微软 GraphRAG 留作"大规模全局报告"的观察项；Neo4j/Kuzu 只在你明确要多跳+Cypher 时再考虑。
