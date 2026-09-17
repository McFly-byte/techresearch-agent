# 总览：DeepResearchAgent 记忆 / 知识图谱 / 飞书 选型总结

> 调研日期：2026-09-16。面向：技术方案调研 Agent（Python + LangGraph + FastAPI + React，3 个月单人，最终开源）。
> 详细论证见同目录 `memory_selection.md`、`knowledge_graph.md`、`feishu_api.md`。
> 下表 Star 数为 GitHub API 2026-09 快照，2026 年第三方报道普遍更高（已在各文档注明）。

---

## 一图看懂最终选型

| 方向 | MVP（现在就上） | 进阶（跑通后再上） |
|---|---|---|
| **记忆 / 上下文压缩** | LangGraph 原生：Checkpointer（短期）+ Store（长期）+ `trim_messages`/summarize 节点 | 在 Store 之上叠加 **Mem0**（自动抽事实） |
| **知识图谱** | **不上图库**：向量 RAG（Chroma/LanceDB）+ LLM 抽 JSON 实体/关系/对比表 | **LightRAG**（MIT、零服务、增量建图） |
| **飞书输出** | **lark-oapi + tenant_access_token**：建 docx + 分批写 blocks | 加协作者/分享、Markdown 富格式转换 |

---

## 方向 1：分层记忆与上下文压缩

**MVP 推荐：LangGraph 原生三件套**
- 短期会话：`MemorySaver`（开发）→ `PostgresSaver`（生产），按 `thread_id` 断点续跑。
- 长期事实/偏好：`InMemoryStore`（开发）→ `PostgresStore`+pgvector（生产），分层 namespace 存 KV。
- 上下文压缩：`trim_messages`（每轮裁剪）+ 自定义 `summarize_node`（超阈值把旧消息压成 `summary` 字段并用 `RemoveMessage` 删除）。
- 理由：零新依赖、零新服务、与 LangGraph 同构、学习成本最低、state 可观测好写评测。

**进阶推荐：Mem0**
- Apache-2.0、纯库、默认本地 Chroma，自动从对话抽/去重/覆盖事实，官方 LangGraph 集成。
- 比 Zep/Graphiti（要 Postgres+图）、Letta（另一套 agent 运行时）侵入性小得多。

**关键源码 / 文档链接：**
- LangGraph Store 官方文档：https://docs.langchain.com/oss/python/langgraph/stores ；API：https://reference.langchain.com/python/langgraph/store/
- LangGraph 记忆 how-to：https://docs.langchain.com/oss/python/langgraph/add-memory
- `trim_messages` 源码：`langchain-core` 仓库 `libs/core/langchain_core/messages/utils.py`
- Mem0：https://github.com/mem0ai/mem0 （LangGraph 集成：仓库内 `mem0/integrations/langgraph/`，文档 https://docs.mem0.ai/open-source/langgraph ）
- 备选观察：Graphiti（Zep 开源核心）https://github.com/getzep/graphiti ；Letta https://github.com/letta-ai/letta ；Cognee https://github.com/topoteretes/cognee

---

## 方向 2：知识图谱集成

**MVP 推荐：不上知识图谱数据库。**
- 用向量 RAG（Chroma/LanceDB，本地零服务）做检索；让 LLM 在出报告时额外输出结构化 JSON（entities / relations / comparisons），前端渲染对比表与关系图。
- 理由：调研 Agent 80% 的"图谱价值"是结构化对比，不是多跳图遍历；图库+schema+抽取流水线对单人项目过重。

**进阶推荐：LightRAG（HKUDS/LightRAG）**
- MIT、纯 Python 库、默认 SQLite+FAISS 零服务、增量建图、dual-level 检索，包成 LangGraph 一个检索工具节点即可。
- 不选微软 GraphRAG 的原因：预跑社区报告，token 成本高、增量弱，对单人项目过重；Neo4j 要常驻服务；Kuzu 是嵌入式零件但不给自动建图流水线。

**关键源码 / 文档链接：**
- LightRAG：https://github.com/HKUDS/LightRAG （论文 arXiv:2410.05779；包 `lightrag-hku`）
- Microsoft GraphRAG：https://github.com/microsoft/graphrag （文档 https://microsoft.github.io/graphrag/ ）
- Kuzu：https://github.com/kuzudb/kuzu （LangChain 集成 `langchain-kuzu`）
- Neo4j：https://github.com/neo4j/neo4j （集成 `langchain-neo4j`）

---

## 方向 3：飞书文档 API 接入

**推荐方案：官方 `lark-oapi` SDK + 自建应用 `tenant_access_token`。**
- 认证：`app_id`+`app_secret` → `POST /open-apis/auth/v3/tenant_access_token/internal`（SDK 自动缓存刷新，别自己存 token）。
- 写文档：`POST /open-apis/docx/v1/documents` 建文档 → `POST .../docx/v1/documents/{id}/blocks/{block_id}/children` 分批写内容块（根块 block_id = document_id）。
- 权限：`docx:document`（创建及编辑新版文档）+ `drive:drive`（云空间），发布版本后生效。
- 手动一次性：建自建应用 → 拿 App ID/Secret → 开权限 → 发布（管理员审批）→ 取 `folder_token` → 把应用加为该文件夹可编辑协作者。

**关键链接：**
- 开放平台：https://open.feishu.cn/
- 取 token：https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
- 创建文档：https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/create
- 写块（children）：https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/create
- Python SDK：https://github.com/larksuite/oapi-sdk-python （`pip install lark-oapi`）
- 最小代码骨架见 `feishu_api.md` 第 6 节。

---

## 给新人的落地顺序（建议）

1. **第 1 步（本周）**：先把记忆层跑起来——LangGraph `MemorySaver` + `InMemoryStore` + `trim_messages`，这是 LangGraph 官方文档里就有的，不引任何第三方。
2. **第 2 步**：调研结果先用向量库检索 + LLM 抽 JSON 表，别急着上图谱。
3. **第 3 步**：用 lark-oapi 把报告写进飞书（手动把应用/权限/folder 配好）。
4. **跑通后**：再按"进阶"列引入 Mem0（长期事实）与 LightRAG（真图检索），作为 2.0 版本的亮点写进开源 README。

> 一句话：**MVP 全部走"官方原生 + 零服务本地后端"，把 3 个月花在调研质量和报告产出上，而不是运维第三方记忆/图服务。**
