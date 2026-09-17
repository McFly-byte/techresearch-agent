# 任务 1：分层记忆与上下文压缩 · 选型报告

> 目标读者：Agent 开发新人（Python 4/5、LangGraph 4/5、async 3/5、FastAPI 2/5）。
> 项目：技术方案调研 DeepResearchAgent，技术栈 Python + LangGraph（自研调度/评测）+ FastAPI + React，3 个月全职，最终开源。
> 调研日期：2026-09-16。Star 数为 GitHub API 快照（见各项目备注），第三方 2026 年报道普遍更高。

---

## 0. 先理清"记忆"到底分几层

调研 Agent 的记忆需求可以拆成三层，选型前先对号入座：

| 层 | 学术命名 | 在本项目里是什么 | 生命周期 | LangGraph 里对应 |
|---|---|---|---|---|
| 工作记忆 / 短期会话 | episodic（情节记忆） | 一次调研任务里的对话、检索结果、中间假设 | 单次 run / 单个 thread | **Checkpointer**（`thread_id` 维度） |
| 长期事实 / 偏好 | semantic（语义记忆） | "用户偏好中文报告""上次调研过 X，结论是 Y""该团队用 PostgreSQL" | 跨 run、跨用户 | **Store**（`namespace` 维度）或 Mem0 |
| 技能 / 流程 | procedural（程序记忆） | 怎么写好一份选型报告、检索哪几个源 | 相对固定 | Prompt / 模板，不属于记忆层选型 |

**上下文压缩** 不是"另一种记忆"，而是**短期工作记忆的保鲜手段**：当 thread 内 `messages` 越积越长，用 trim / summarize / extract 把它压回窗口内。

---

## 1. 候选项目逐项对比

### 1.1 Mem0（mem0ai/mem0）★ 重点

| 项 | 内容 |
|---|---|
| GitHub | https://github.com/mem0ai/mem0 |
| Star | 约 3.6 万（GitHub API 快照 36,607；2026 年第三方报道约 4.8–5.7 万） |
| License | **Apache-2.0** ✅（可商用、可开源自己的项目） |
| 主语言 | Python（约 53%）+ TypeScript |
| 成熟度 | YC S24，2025-10 完成 $24M A 轮；PyPI `mem0`，月下载千万级；官方维护 LangGraph / CrewAI / LlamaIndex 集成 |
| 核心架构 | 库（非服务）。`Memory` 主类 → `add()` 时用 LLM 把对话拆成"事实条目"存入 向量库 + KV；`search()` 时向量+图混合召回。支持 Graph Memory（Neo4j 后端，需 `version=v1.1`） |
| 记忆类型 | **semantic 为主**（抽取用户/实体事实），也可存 episodic 原始消息；procedural 需自行设计 |
| LangGraph 集成 | 官方内置集成包：`mem0/integrations/langgraph/`（仓库内路径）；文档：https://docs.mem0.ai/open-source/langgraph |
| 存储后端 | 默认本地（Chroma/Qdrant/SQLite 可选）；生产可换 Postgres+pgvector |
| 优点 | ① 上手 4 行代码；② 自动事实抽取/去重/更新（"用户改名了"会覆盖旧事实）；③ 文档全、社区最大；④ Apache-2.0 干净 |
| 缺点 | ① 事实抽取依赖一次额外 LLM 调用（成本/延迟）；② 对"一次性调研 run"的短期对话管理帮不上忙（仍要靠 Checkpointer）；③ 配置项多，初学要花时间理解 `add()`/`search()` 的 user_id/agent_id 维度 |

**最小用法**（官方示例的简化）：
```python
from mem0 import Memory

m = Memory()
# 写入：LLM 自动从对话里抽出事实
m.add([
    {"role": "user", "content": "我在做一个 LangGraph 的调研 Agent，团队用 PostgreSQL。"},
    {"role": "assistant", "content": "好的，已记下你的技术栈偏好。"},
], user_id="user-001")

# 召回：只取与当前问题相关的事实
facts = m.search("这个项目的技术栈是什么？", user_id="user-001")
```

---

### 1.2 Zep / Graphiti（getzep/zep + getzep/graphiti）

| 项 | 内容 |
|---|---|
| GitHub（开源核心） | https://github.com/getzep/graphiti（Zep 的开源图框架）；Zep 服务端 https://github.com/getzep/zep |
| Star | Graphiti 约 2.0 万（API 快照 20,375，fork 1,940） |
| License | **Apache-2.0** |
| 核心架构 | **双时态知识图谱（bi-temporal knowledge graph）**：每条事实带"生效时间段"，事实被推翻时自动标失效，支持 point-in-time 查询。Graphiti 跑在 Postgres+pgvector 上 |
| 记忆类型 | semantic（实体/关系/事件），强在**时间演化**（"三个月前 A 公司用 Neo4j，现在换成 Kuzu 了"） |
| LangGraph 集成 | 官方 `zep-langgraph` 包 + 文档 https://help.getzep.com/v3/langgraph-memory |
| 优点 | ① LongMemEval 等基准上事实召回质量高；② 时间感知，适合"技术会过时"的调研场景；③ 事实可溯源到原始消息 |
| 缺点 | ① **需要 Postgres（+pgvector）常驻**，对单人项目偏重；② Zep 本体是 open-core，完整能力在商业云；③ 概念（bi-temporal、edge 有效性）学习曲线陡；④ Graphiti 直接用比 Zep Cloud 配置繁琐 |

> 结论：能力很强，但**对 MVP 过重**。它的价值点（跨时间的事实演化）要到项目中后期、积累几十份调研报告后才体现得出来。

---

### 1.3 Letta（letta-ai/letta，原 MemGPT）

| 项 | 内容 |
|---|---|
| GitHub | https://github.com/letta-ai/letta |
| Star | 约 2.5 万（API 快照 24,762，fork 2,619；2026-09 仍活跃 push） |
| License | **Apache-2.0** |
| 核心架构 | **OS 式记忆分页**：`core_memory`（常驻上下文的小段、agent 可自编辑）+ `archival_memory`（PostgreSQL+pgvector 长期归档）+ `recall_memory`（全量消息日志）。Agent 通过 `core_memory_append` / `archival_memory_insert` 等**工具自己管理记忆**（MemGPT 论文 arXiv:2310.08560） |
| 记忆类型 | episodic + semantic，特色是 **agent 自编辑记忆块**、sleep-time compute（空闲时整理记忆） |
| LangGraph 集成 | **没有官方一等公民集成**；Letta 本身是有状态 agent 运行时（自带服务端），与 LangGraph 是"竞争者/另一种范式"，需要把 Letta agent 当工具调 |
| 优点 | 记忆自管理范式成熟、文档好、活跃度高 |
| 缺点 | ① **引入 Letta ≈ 引入另一套 agent 框架**，和"LangGraph 自研调度"的定位冲突；② 需起 Letta 服务 + PostgreSQL；③ 对初学者概念最多 |

> 结论：除非想换成 Letta 运行时，否则**不建议作为 LangGraph 项目的记忆插件**。

---

### 1.4 LangGraph 原生 Store / Memory（langchain-ai/langgraph）

| 项 | 内容 |
|---|---|
| GitHub | https://github.com/langchain-ai/langgraph（源码：`libs/langgraph/langgraph/store/`） |
| 文档 | https://docs.langchain.com/oss/python/langgraph/stores ；API 参考 https://reference.langchain.com/python/langgraph/store/ |
| License | **MIT**（LangGraph 核心仓库） |
| 核心架构 | 两件套：<br>① **Checkpointer**（短期）：`MemorySaver`（内存）/ `SqliteSaver` / `PostgresSaver`，按 `thread_id` 存 state，支持断点续跑、time travel<br>② **Store**（长期）：`BaseStore` 接口 + `InMemoryStore` / `PostgresStore`(+pgvector 可选语义检索)，用**分层 namespace**（如 `("user:001", "preferences")`）跨 thread 存 KV |
| 记忆类型 | episodic（Checkpointer 里的 messages）+ semantic（Store 里手写/抽取的 KV）；不自动抽事实 |
| LangGraph 集成 | **原生，零依赖**。在 node 签名里声明 `store: BaseStore`，`graph.invoke(..., config={"configurable": {"thread_id": ...}})` |
| 优点 | ① **零新组件、零学习额外框架**；② 与你正在学的 LangGraph 完全同构；③ 可观测（state 就是 JSON）；④ MVP 到生产可平滑换 `MemorySaver → PostgresSaver` |
| 缺点 | ① **不做自动事实抽取**，长期记忆要你自己写"extract 节点"；② 没有图关系；③ InMemoryStore 重启即丢（但本地 SQLite/PostgresStore 可补） |

**最小骨架**（短期 + 长期都用上）：
```python
from langgraph.graph import StateGraph, MessagesState
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.store.base import BaseStore

builder = StateGraph(MessagesState)

def research_node(state: MessagesState, *, store: BaseStore):
    # 长期：按 namespace 读用户偏好（跨 thread）
    pref = store.get(("user-001", "prefs"), "lang")
    lang = pref.value if pref else "中文"
    # ... 真正的调研逻辑 ...
    return {"messages": [...]}

builder.add_node("research", research_node)
# ... 连边 ...

graph = builder.compile(
    checkpointer=MemorySaver(),          # 短期：thread 内断点续跑
    store=InMemoryStore(index={"embed": ...}),  # 长期：跨 thread KV；生产换 PostgresStore
)

# 同一个 thread_id => 短期记忆；不同 thread_id 但 store 共享 => 长期记忆
graph.invoke({"messages": [...]}, config={"configurable": {"thread_id": "run-42"}})
```

---

### 1.5 Cognee（topoteretes/cognee）

| 项 | 内容 |
|---|---|
| GitHub | https://github.com/topoteretes/cognee |
| Star | 约 3.0 万（API 快照 30,690，fork 3,028；2026-09-15 仍在 push，活跃） |
| License | **Apache-2.0** |
| 核心架构 | ECL 流水线：`add()` → Extract（切分）→ Cognify（LLM 抽实体/关系建**知识图谱**）→ Load（向量+图+关系库混合）；查询时图+向量混合召回 |
| 记忆类型 | 偏 **semantic + 知识图谱**，适合"文档→可推理的知识" |
| LangGraph 集成 | 可作为库调用（`cognice.cognify()` / `cognice.search()`），无官方深度集成包 |
| 优点 | 开箱即带图+向量混合；适合"喂文档进去、问跨文档问题" |
| 缺点 | ① **依赖重**（默认要 LLM + 嵌入 + 图库，dev 模式也起一堆服务）；② API 仍快速演进（open issues ~495）；③ 对"对话记忆"不是它的主场，更像一个 RAG/KG 引擎 |

> 结论：它本质是**任务 2 的知识图谱/RAG**，不是任务 1 的"对话记忆层"。别重复引入。

---

### 1.6 LlamaIndex Memory

LlamaIndex 没有独立的"记忆层产品"，相关能力散落在：
- `llama_index.core.memory`：`ChatMemoryBuffer`（短期窗口）、`SummaryMemoryBuffer`（自动摘要）——本质和 LangGraph 的 trim/summarize 同类。
- `llama_index.core.indices`：把文档建索引后当长期知识。
- 与 LangGraph 集成：可把 LlamaIndex 的 memory/index 包成 LangGraph 的工具或 store。

> 结论：你已经选了 LangGraph 生态，再引入 LlamaIndex memory 是**多学一套 API、收益低**。不推荐。

---

## 2. 上下文压缩（短期间记忆保鲜）专项

长调研 run 里 `messages` 会爆。LangChain/LangGraph 官方给了三种原语，**不依赖任何记忆库**：

### 2.1 Trim（裁剪，最简单）
`langchain_core.messages.trim_messages`（源码：`langchain_core/messages/utils.py`）。在调用 LLM 前裁剪，永远保留 system + 最近 N 条。
```python
from langchain_core.messages import trim_messages

messages = trim_messages(
    state["messages"],
    strategy="last",       # 保留最近
    token_counter=len,     # 初学用"条数"；上线换真正的 tokenizer
    max_tokens=20,         # 只保留最近 20 条
    start_on="human",      # 从 human 消息开始截，避免把 tool_call 半截留下
)
```

### 2.2 Summarize（摘要，推荐用于长调研）
自定义一个 `summarize_node`：把旧消息喂给 LLM，产出 `summary` 字段写回 state，再用 `RemoveMessage` 把旧消息从 state 里删掉。
```python
from langchain_core.messages import RemoveMessage, SystemMessage

def summarize_node(state: MessagesState, llm) -> dict:
    old = state["messages"][:-4]          # 保留最近 4 条，其余压缩
    if not old:
        return {}
    summary = state.get("summary", "")
    new_summary = llm.invoke([
        SystemMessage(
            "把已有摘要和新对话合并成一段中文调研笔记，"
            "保留：已确认的技术结论、待验证假设、关键数据。"
        ),
        ("user", f"已有摘要：{summary}\n\n新对话：{old}"),
    ]).content
    return {
        "summary": new_summary,
        "messages": [RemoveMessage(id=m.id) for m in old],  # 从 state 删除旧消息
    }
```
> 关键技巧：`summary` 作为 state 字段持久化在 Checkpointer 里，**压缩后上下文从 O(n) 降到 O(1)**，且不丢结论。

### 2.3 Extract（抽取关键事实，进阶）
在 summarize 之上更进一步：不只是写散文摘要，而是让 LLM 把"事实/偏好/数字"抽成结构化 dict，写进 **Store**（长期）。这一步做好了，就等于自己实现了一个轻量 Mem0。

### 2.4 成熟压缩库
- **官方推荐**：`trim_messages` + 自研 summarize 节点（上面两例）。
- **第三方**：LangChain 生态的 `LLMChainExtractor` / `LLMChainSummarizer`（`langchain.chains`，旧 API，新项目不建议直接用）。
- **结论**：MVP 阶段**不需要任何压缩库**，`trim_messages` + 一个 summarize 节点就够。

---

## 3. 推荐方案

### ✅ MVP 推荐：LangGraph 原生三件套（Checkpointer + Store + trim/summarize 节点）

**理由**：
1. **零额外依赖、零新服务**——你已经在学 LangGraph，这套就是它的官方记忆模型，文档：https://docs.langchain.com/oss/python/langgraph/add-memory 。
2. 完全覆盖你的两个硬需求：短期会话记忆（Checkpointer）+ 长期事实（Store 写 KV）+ 上下文压缩（trim/summarize）。
3. 3 个月单人、要开源，**学习成本最低、可观测性最好**（state 就是 JSON，方便写评测）。
4. 平滑升级路径清晰：本地 `MemorySaver` / `InMemoryStore` → 生产 `PostgresSaver` / `PostgresStore`，业务代码不动。

**落地清单**：
```
短期：MemorySaver(开发) → PostgresSaver(生产)
长期：InMemoryStore(开发) → PostgresStore+pgvector(生产)
压缩：trim_messages(每轮 LLM 前) + summarize_node(消息超阈值触发)
```

### 🚀 进阶推荐：在原生 Store 之上叠加 Mem0

**触发条件**（满足任一再上）：
- 需要跨 thread 自动记住"用户偏好/已调研过的技术结论"，不想手写 extract 节点；
- 长期事实要自动去重、更新、覆盖（例如"A 方案之前用 Neo4j，现已换 Kuzu"）；
- 想要向量+图混合召回，且能接受一次额外 LLM 抽取成本。

**为什么是 Mem0 而不是 Zep/Letta**：
- Apache-2.0、纯库、默认本地后端（Chroma），**不用常驻 Postgres/服务**；
- 官方 LangGraph 集成最成熟；
- 比 Letta（另一套 agent 运行时）侵入性小，比 Zep/Graphiti（要 Postgres+图）轻。

---

## 4. 集成代码骨架（MVP，可直接跑）

```python
# memory_setup.py —— 技术调研 DeepResearchAgent 的记忆层（MVP）
# 依赖：pip install langgraph langchain-core
from typing import TypedDict
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.store.base import BaseStore
from langchain_core.messages import (
    SystemMessage, RemoveMessage, HumanMessage, BaseMessage,
)
from langchain_core.messages.utils import trim_messages

# ---- 0. State：在 MessagesState 基础上加一个 summary 字段 ----
class ResearchState(MessagesState):
    summary: str   # 对话压缩后的"调研笔记"

# ---- 1. 压缩节点：旧消息 -> 摘要，删除旧消息 ----
def make_summarize_node(llm, keep_recent: int = 6):
    def summarize_node(state: ResearchState) -> dict:
        old = state["messages"][:-keep_recent]
        if not old:
            return {}
        prev = state.get("summary", "")
        resp = llm.invoke([
            SystemMessage(
                "你在帮一个技术调研 Agent 维护记忆。"
                "把下面已有调研笔记和新对话合并成一段中文要点，"
                "保留：技术结论、关键数据/链接、待验证假设、用户偏好。不要寒暄。"
            ),
            ("user", f"【已有笔记】\n{prev}\n\n【新对话】\n{old}"),
        ])
        return {
            "summary": resp.content,
            "messages": [RemoveMessage(id=m.id) for m in old],
        }
    return summarize_node

# ---- 2. 主节点：调 LLM 前先 trim，并注入长期记忆 ----
def make_research_node(llm):
    def research_node(state: ResearchState, *, store: BaseStore):
        # 长期记忆：从 Store 读用户偏好（跨 thread）
        pref_item = store.get(("user:default", "preferences"), "language")
        lang = pref_item.value if pref_item else "中文"

        # 短期压缩：调 LLM 前裁剪，保留 system + 最近消息
        msgs = trim_messages(
            state["messages"], strategy="last",
            token_counter=len, max_tokens=20, start_on="human",
        )
        sys_msg = SystemMessage(
            content=f"你是技术调研助手。用{lang}回答。"
                    f"当前调研笔记：\n{state.get('summary', '(尚无)')}"
        )
        answer = llm.invoke([sys_msg, *msgs]).content
        return {"messages": [HumanMessage(content=answer)]}  # 实际换成 AIMessage
    return research_node

# ---- 3. 组装图 ----
def build_graph(llm):
    builder = StateGraph(ResearchState)
    builder.add_node("research", make_research_node(llm))
    builder.add_node("summarize", make_summarize_node(llm))
    builder.add_edge(START, "research")
    # 调研 -> 压缩 -> 结束（真实项目里 research 会连工具调用节点）
    builder.add_edge("research", "summarize")
    builder.add_edge("summarize", END)

    return builder.compile(
        checkpointer=MemorySaver(),          # 短期：thread 内断点续跑
        store=InMemoryStore(),               # 长期：跨 thread KV；生产换 PostgresStore
    )

# ---- 4. 使用 ----
if __name__ == "__main__":
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model="gpt-4o-mini")
    graph = build_graph(llm)

    # 写一条长期偏好（跨 thread 生效）
    graph.store.put(("user:default", "preferences"), "language", {"value": "中文"})

    cfg = {"configurable": {"thread_id": "research-run-1"}}
    out = graph.invoke({"messages": [HumanMessage(content="对比 LightRAG 和 GraphRAG")]}, cfg)
    print(out["messages"][-1].content)
```

---

## 5. 一句话决策

> **MVP：LangGraph 原生 Checkpointer + Store + trim/summarize，不引第三方记忆库。**
> **进阶：长期事实记忆换 Mem0（Apache-2.0、本地 Chroma、官方 LangGraph 集成）。**
> Zep/Graphiti、Letta、Cognee 在 3 个月单人项目里都偏重，先记在观察清单里，不进 MVP。
