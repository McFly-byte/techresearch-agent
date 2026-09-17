# TechResearch Agent 分阶段实施 Prompts

> 用途：将本文件中的某一个阶段 Prompt 单独复制给 AI，让 AI 在当前项目中只完成该阶段。
>
> 项目根目录：`D:\Programs\Py_proj\DeepResearch Agent`
>
> 方案依据：`docs/技术设计文档.md`（v0.2）及 `research/` 下的事实审计和专项调研资料。
>
> 重要说明：本文是实施指令，不是新的技术设计。技术方案冲突时，以已评审的 `docs/技术设计文档.md` 为主；若设计文档与当前依赖的官方文档或真实代码不一致，必须先给出证据、影响和最小修订建议，不能自行猜测。

---

## 1. 建议使用方式

1. 从“阶段 0”开始，一次只复制一个阶段的 Prompt。
2. AI 完成当前阶段并提交验证结果后，再执行下一阶段。
3. 如果当前仓库已经完成了某些任务，也不能直接跳过检查；AI 应先核实现状，再只补齐缺口。
4. 阶段验收未通过时，不要执行后续阶段。
5. API Key、账号开通、飞书应用权限等必须由用户完成的操作，AI 只能给出明确清单，不得把真实密钥写入仓库。
6. 建议每阶段完成后创建一个独立 Git commit，但 AI 未经明确授权不得向远程仓库 push。

### 推荐顺序

| 阶段 | 建议周期 | 核心结果 |
|---|---:|---|
| 阶段 0 | 1～2 天 | 基线审计、工程契约、可运行空骨架 |
| 阶段 1 | 第 1～2 周 | 搜索→抓取→事实提取→简单报告的纵向切片 |
| 阶段 2 | 第 3 周 | 类型化状态、Planner、Supervisor-Worker 并行编排 |
| 阶段 3 | 第 4 周 | Worker 循环、反思、失败恢复、预算控制、断点恢复 |
| 阶段 4 | 第 5～6 周 | 引用验证、报告生成、记忆、混合检索和导出 |
| 阶段 5 | 第 7～8 周 | 评测集、基线、消融实验、失败分析与图表 |
| 阶段 6 | 第 9～10 周 | FastAPI、SSE、React 四页 Web 应用和 CLI |
| 阶段 7 | 第 11～12 周 | 全链路测试、优化、文档、开源与面试材料 |

---

## 2. 所有阶段共用的强制执行规则

以下规则已经写入每个 Prompt 的目标中。若使用的 AI 支持项目级系统指令，也可以把本节作为长期指令。

### 2.1 事实与源码优先

- 开始修改前，必须完整阅读当前阶段涉及的现有源码、测试、配置和 `docs/技术设计文档.md` 对应章节。
- 不得仅凭设计文档中的示例代码猜测当前依赖的真实 API；涉及 LangGraph、LangChain、Qwen/DashScope、LangSmith、Tavily、Chroma、FastAPI、React 或飞书 SDK 时，以项目锁定版本的源码、类型定义、官方文档和真实运行结果为准。
- 不得编造模型 ID、API 返回字段、评测分数、成本、延迟、准确率或免费额度。
- 设计文档中的“预期值”“面试示例值”不能被当作真实实验结果。

### 2.2 范围控制

- 只实现当前阶段明确列出的内容，不提前实现后续阶段。
- 不得擅自引入 Celery、Redis、多用户系统、Docker、云部署、GitHub 源码研究、LightRAG、MCP Server、多模型交叉验证等非当前 MVP 内容。
- 优先修改已有文件；创建新文件前先确认仓库中没有功能重复的实现。
- 不得复用旧 Paper-Agent 的代码；它只能作为反例或迁移对照。

### 2.3 工程质量

- Python 使用 3.11+，保持完整类型标注；异步边界必须清晰，不在 async 路径中直接执行长时间阻塞 I/O。
- 数据模型使用 Pydantic；外部服务通过接口或适配器隔离，业务核心不得直接读取散落的环境变量。
- 测试不得默认访问真实付费 API。外部 API、LLM、时间和网络都应能被 fake/mock 替换。
- 每个 bug 修复都应补充能复现该 bug 的测试。
- 日志不得记录 API Key、完整敏感请求头或用户私有文档正文。
- 所有错误必须保留可诊断上下文，但向 API 用户返回稳定、结构化的错误格式。

### 2.4 验证与交付

每阶段结束前，AI 必须实际执行并报告：

1. 与本阶段最相关的单元测试。
2. 全量后端测试；已有前端后执行前端测试、类型检查和构建。
3. lint、format check、type check；若仓库尚无对应工具，只能在阶段 0 建立，后续沿用。
4. 一个真实入口的 smoke test；无密钥时使用 fake provider，不得伪称真实联网成功。
5. `git diff --check` 或等效检查。
6. 回读本阶段产物，确认没有未完成占位冒充完成、没有真实密钥、没有伪造数据。

每阶段的最终回复必须包含：

- 完成内容；
- 修改文件清单；
- 关键设计取舍；
- 实际执行的验证命令及结果；
- 未完成项或阻塞项；
- 需要用户手动完成的操作；
- 是否满足进入下一阶段的门禁。

---

# 阶段 0 Prompt：仓库审计、工程契约与基础骨架

```text
你现在负责 TechResearch Agent 的阶段 0。项目根目录是：
D:\Programs\Py_proj\DeepResearch Agent

你的任务不是直接实现全部产品，而是建立后续各阶段可安全迭代的工程基线。

【开始前必须完成】
1. 完整阅读 docs/技术设计文档.md，重点阅读第 1、2、3、11、13、14、15、16 章。
2. 检查项目当前目录、Git 状态、已有源码、测试、依赖与配置。不要假定仓库仍然只有文档。
3. 阅读 research/04_fact_audit/ 下与模型 API、LangSmith、DeepResearch Bench II 有关的审计结论。只采用有证据的事实。
4. 检查当前可用 Python、Node.js 和包管理工具版本；记录事实，不要臆测。
5. 若工作区存在用户未提交的修改，不得覆盖、删除或重置。

【本阶段目标】
A. 建立与技术设计一致但不过度实现的项目骨架。
B. 建立统一的工程规范、依赖管理、配置加载、测试和本地启动方式。
C. 建立可替换的 provider 接口，使后续无真实 API Key 时也能通过 fake provider 测试。
D. 输出真实的项目基线说明和用户手动操作清单。

【实施内容】
1. 根据仓库现状补齐合理目录，但不要机械创建设计文档里每一个未来文件。只创建本阶段和阶段 1 必需的目录与 __init__.py。
2. 配置 Python 项目：
   - pyproject.toml；
   - Python 3.11+ 约束；
   - 运行依赖与开发依赖分组；
   - pytest、异步测试、lint、format、type check 的确定性命令。
3. 建立 src/core/config.py：
   - 使用 pydantic-settings；
   - 支持 .env；
   - 对必填和可选配置作清晰区分；
   - 不得硬编码真实 Key；
   - 提供 .env.example 和安全的 .gitignore。
4. 建立模型 provider 抽象和最小模型工厂：
   - 业务模块依赖抽象接口，不直接绑定厂商；
   - 至少提供 FakeLLM，保证测试不联网；
   - Qwen/DashScope 实现只做最小可初始化适配，不在没有 Key 时发真实请求；
   - model id 从配置读取，不在业务代码写死；
   - 如果当前 SDK 对 reasoning/thinking 字段的处理与设计文档不一致，以真实 SDK/官方文档为准，并记录差异。
5. 建立统一异常、日志和 request/task correlation id 的基础设施。
6. 建立最小 FastAPI 应用，仅实现 /api/health；不得提前实现研究任务 API。
7. 建立最小 CLI 入口，可执行 health/config doctor；输出不得泄露密钥。
8. 初始化 React + TypeScript + Vite 的最小工程壳（如果仓库尚无 web 工程），只保证能启动和构建，不实现业务页面。
9. 建立 tests/：
   - 配置加载测试；
   - 缺失密钥时的行为测试；
   - health API 测试；
   - FakeLLM 测试；
   - 敏感配置不出现在日志/序列化输出中的测试。
10. 新建 docs/development.md，写清 Windows 环境下的安装、测试、启动命令。
11. 新建 docs/manual-setup.md，只列出需要用户手动申请或填写的配置，明确密钥只能写入本地 .env。
12. 新建 docs/implementation-progress.md，记录阶段 0 的实际结果、验证命令和待办。这里是后续阶段唯一的进度交接文档。

【明确禁止】
- 不实现真实搜索、Supervisor、Worker、RAG、引用验证、记忆、评测或完整前端。
- 不购买、申请或猜测任何 API Key。
- 不把 qwen3.8-max 等模型名称当作永远正确的常量；配置默认值必须能被覆盖，并说明证据来源。
- 不删除 research/ 和 docs/ 中的现有资料。
- 不 push、不创建远程资源。

【验收标准】
1. 新环境能按 docs/development.md 安装项目。
2. 后端 health API 可启动并返回稳定 JSON。
3. CLI doctor 能区分“本地可运行”和“缺少可选外部 Key”。
4. 无任何外部 Key 时，测试、lint、type check 均可运行。
5. 前端能完成类型检查和生产构建。
6. 仓库扫描不到真实密钥或明显秘密值。
7. docs/implementation-progress.md 能让下一位 AI 准确知道当前实现状态。

【完成方式】
先探查，再列出不超过 10 项的执行计划，然后直接实施。若发现会改变架构的关键冲突，先给出证据和最小决策问题；普通工程细节自行按现有约定处理。完成后执行验证，并按“完成内容/文件清单/取舍/验证/手动操作/阻塞/阶段门禁”汇报。不要开始阶段 1。
```

---

# 阶段 1 Prompt：工具层与单流程纵向切片

```text
你现在负责 TechResearch Agent 的阶段 1。项目根目录是：
D:\Programs\Py_proj\DeepResearch Agent

本阶段目标是跑通第一个可验证的纵向切片：输入一个技术问题，经过搜索、抓取、事实提取，输出一份简单 Markdown 报告。暂时不做多智能体。

【进入阶段的前置门禁】
1. 阅读 docs/implementation-progress.md 和 git diff，确认阶段 0 已完成且没有未解释的失败。
2. 完整阅读 docs/技术设计文档.md 第 2、3、5、10、11、13、14、15 章相关内容。
3. 阅读当前 provider、配置、异常、日志、API 和测试代码，以源码为准。
4. 核对本项目实际锁定版本的 Tavily、arXiv、Trafilatura、LangChain/LangGraph API；不得照抄设计文档伪代码。
5. 若前置门禁不满足，只修复阶段 0 的阻塞，不得继续扩展功能。

【本阶段目标】
A. 实现统一、可替换、可测试的研究工具接口。
B. 实现单流程 search → fetch/read → extract facts → write report。
C. 为后续多 Agent 保留结构化 Fact、Citation 和工具结果，不让字符串拼接成为核心数据协议。
D. 接入 LangSmith 的可选 tracing，但无 Key 时必须完全可运行。

【实施内容】
1. 定义并测试结构化领域模型：
   - SourceDocument；
   - Citation；
   - Fact；
   - SearchResult；
   - SimpleResearchResult。
   字段应满足可追溯性：URL/来源标识、标题、原文片段、抓取时间、提取时间、错误状态。
2. 定义 SearchProvider、PageFetcher、PaperSearchProvider、FactExtractor、ReportWriter 协议或抽象接口。
3. 实现工具适配器：
   - Tavily Web 搜索；
   - arXiv 搜索；
   - Trafilatura 网页正文提取；
   - 本地 Markdown 读取；
   - 本地 PDF 解析可先实现可靠的文本型 PDF 路径，扫描件 OCR 不在本阶段。
4. 每个外部工具必须具备：
   - 明确超时；
   - 有上限的重试；
   - 结构化错误；
   - URL 基础校验；
   - 响应大小上限或截断策略；
   - 可注入 fake 实现。
5. 实现 SimpleResearchService：
   - 输入 query 与数据源配置；
   - 生成少量搜索 query；
   - 搜索并去重；
   - 抓取或读取内容；
   - 提取带 snippet 和 source id 的事实；
   - 只依据结构化事实生成 Markdown 报告；
   - 报告引用能回溯到来源记录。
6. 若引入 LangGraph，只搭建线性的 typed graph，不实现 Supervisor-Worker、动态重规划或 Send API。
7. 为 LangSmith 增加可选 tracing：
   - 未配置时不报错；
   - 不把文档全文或秘密信息无条件写入 metadata；
   - trace tags 能区分 search/fetch/extract/write。
8. 提供 CLI 命令运行 simple research：
   - fake 模式必须开箱即用；
   - live 模式缺 Key 时给出明确、可操作的错误。
9. 可选增加最小 API 入口，但若阶段 0 只有 health，本阶段只需保证 service 可被 API 调用；不要提前实现异步任务管理和 SSE。
10. 增加单元测试、契约测试和一个 fake provider 端到端测试。
11. 更新 docs/development.md、docs/manual-setup.md、docs/implementation-progress.md。

【明确禁止】
- 不实现多 Worker 并行、Supervisor、反思循环、预算降级、引用 NLI 验证、向量数据库或完整 React 页面。
- 不在测试中调用付费 API。
- 不把 Tavily 返回的摘要直接冒充“已回查原文”。
- 不生成无 source id 的事实；不允许 Writer 自行增加输入中不存在的引用。
- 不把网络失败吞掉后返回“成功但空报告”。

【验收标准】
1. fake 模式能从固定输入完整生成 Markdown 报告。
2. 报告中的每个引用编号都能映射到唯一 Citation，Citation 有 URL/来源标识和 snippet。
3. 搜索去重、超时、空结果、抓取失败、非法 URL、PDF 解析失败均有测试。
4. 无 Key 时全量测试通过；有 Key 时可以通过显式 live 标记运行联网 smoke test，但不得把 live test 纳入默认测试。
5. LangSmith 未配置时不影响功能。
6. 所有新增公共接口有类型标注和最小文档。

【完成方式】
先检查阶段 0 门禁和当前实现，再给出计划并实施。优先做小的纵向切片，不要一次堆满所有适配器再联调。每增加一个适配器先写 fake/契约测试。完成后实际运行 fake 端到端示例，保存一份非伪造的测试产物到 tests/fixtures 或临时测试目录，不要把它宣传为真实调研报告。最后更新进度文档并停止，不得进入阶段 2。
```

---

# 阶段 2 Prompt：LangGraph 状态与 Supervisor-Worker 编排

```text
你现在负责 TechResearch Agent 的阶段 2。项目根目录是：
D:\Programs\Py_proj\DeepResearch Agent

本阶段只实现核心编排：类型化状态、Planner、Supervisor、自研路由逻辑和多 Worker 并行。Worker 内部的高级反思、预算降级和完整失败恢复留到阶段 3。

【进入阶段的前置门禁】
1. 阅读 docs/implementation-progress.md，确认阶段 1 的 fake 端到端测试通过。
2. 阅读 docs/技术设计文档.md 第 2、3、4.1、4.2、6.1、6.2、14、15 章。
3. 阅读阶段 1 的领域模型、工具协议和 SimpleResearchService，优先复用，不得重复定义语义冲突的数据模型。
4. 核对当前锁定 LangGraph 版本中 StateGraph、Command、Send、reducer、subgraph 和并行分支的真实 API。

【本阶段目标】
A. 用明确的 typed state 表达研究任务，而不是用自由文本聊天历史承载全部状态。
B. 自行实现 Planner/Supervisor 调度，不直接复制官方 Open Deep Research 或 supervisor 包的核心路由。
C. 支持 3～5 个无依赖子任务并行执行，并将结果确定性聚合。
D. 支持任务依赖，依赖未满足时不得执行下游任务。

【实施内容】
1. 基于当前代码统一设计并实现：
   - ResearchState；
   - SubTask；
   - WorkerState；
   - ReportState 或当前阶段所需的最小子集；
   - 明确的状态 reducer。
2. 所有 list/dict 合并都必须定义并发语义：
   - 如何去重；
   - 顺序是否稳定；
   - 同一 task_id 重复回传如何处理；
   - 失败任务是否覆盖成功任务。
3. 实现 Planner：
   - 输入 query、user_context、research_depth；
   - 输出结构化子任务；
   - quick/standard/deep 的数量边界；
   - 每个子任务包含目标、范围、预期产出、依赖、优先级；
   - 结构化输出解析失败时有一次受控修复，仍失败则返回明确错误。
4. 实现自研 Supervisor：
   - pending/running/completed/failed 状态转换；
   - max_workers 并发上限；
   - 依赖检查；
   - dispatch/wait/finish 的显式决策；
   - 同一任务只能完成一次；
   - 不出现死循环。
5. 使用 LangGraph 当前版本支持的官方机制动态分发 Worker；若 Send API 行为与设计文档不同，记录真实差异并按当前 API 实现。
6. Worker 本阶段复用阶段 1 的 search/read/extract/simple summary 能力，不做高级反思。
7. 聚合输出必须保留每个事实和引用的来源子任务，避免并行合并后失去 provenance。
8. 增加图结构/路由测试：
   - 3 个无依赖任务并行；
   - 有依赖任务等待；
   - 并发上限生效；
   - 一个 Worker 失败，其他 Worker 不被无故取消；
   - 重复 task_id；
   - 空计划；
   - 解析失败；
   - 确定性聚合。
9. 增加 fake 模式端到端测试：给定技术选型问题，Planner 生成多个任务，Worker 执行并聚合为多章节草稿。
10. 更新架构说明和 docs/implementation-progress.md，明确当前“动态重规划尚未实现”或真实完成度。

【明确禁止】
- 不直接引入 langgraph-supervisor 的成品路由替代自研调度。
- 不实现阶段 3 的复杂反思、预算阈值切模型和持久化恢复。
- 不实现 CitationVerifier、Chroma、评测系统或完整前端。
- 不用 sleep 来掩盖竞态问题。
- 不依赖真实 LLM 验证路由正确性；核心调度必须能用 deterministic fake 测试。

【验收标准】
1. 状态模型与 reducer 有单元测试，并发合并结果稳定可复现。
2. Planner 输出非法结构时能安全失败或受控修复。
3. 至少一个测试证明多个无依赖 Worker 实际并发，而不是仅逻辑上标为并行。
4. 至少一个测试证明依赖任务不会提前执行。
5. 单 Worker 失败不会污染其他任务结果，最终状态能区分部分成功和整体失败。
6. fake 端到端结果能显示子任务、各自事实、引用和汇总草稿。

【完成方式】
先画出当前代码对应的状态流和依赖关系，再实施。调度核心尽量保持纯函数，使路由规则可独立测试。任何状态字段改名都要同步测试和文档。完成验证后更新进度文档并停止，不得进入阶段 3。
```

---

# 阶段 3 Prompt：研究循环、反思、失败恢复、预算与持久化

```text
你现在负责 TechResearch Agent 的阶段 3。项目根目录是：
D:\Programs\Py_proj\DeepResearch Agent

本阶段在已完成的 Supervisor-Worker 编排上加入“可控的长程执行能力”：Worker 迭代、反思式 query 改写、分级失败恢复、预算感知、动态重规划和 checkpoint 恢复。

【进入阶段的前置门禁】
1. 阅读 docs/implementation-progress.md，确认阶段 2 的并行、依赖和失败隔离测试全部通过。
2. 阅读 docs/技术设计文档.md 第 3.6、4.1、4.5、6.2、6.4、10、14、15、16 章。
3. 阅读当前 Supervisor、Worker、provider、工具错误模型与测试，不得另起一套状态机。
4. 核对当前 LangGraph checkpointer 和持久化包的真实接口；MemorySaver/InMemorySaver/SQLite saver 名称以锁定版本为准。

【本阶段目标】
A. Worker 能多轮检索并根据证据充分度决定继续或停止。
B. 失败恢复按错误类型处理，重试有上限，不能无限循环。
C. token、搜索轮次、时间/调用次数预算能在全局和子任务层被执行，而不只是记录。
D. 任务可从 checkpoint 恢复，恢复后不重复执行已确认完成的副作用。
E. Supervisor 能基于新证据受控追加子任务。

【实施内容】
1. 实现 Worker 循环：plan query → search → fetch/read → extract → reflect → continue/stop。
2. 定义结构化 ReflectionResult：
   - quality；
   - evidence_gaps；
   - next_queries；
   - should_stop；
   - stop_reason；
   - confidence。
3. 反思判断必须基于可观察信号：相关结果数、独立来源数、未覆盖问题、矛盾事实、失败次数和预算余量。不能只让 LLM 自由发挥。
4. 实现 query 改写：too_broad、too_narrow、off_track、conflicting_evidence 等有限类别；记录每轮 query，防止原样重复。
5. 实现失败分类和策略：
   - timeout/临时网络错误：指数退避和有上限重试；
   - rate limit：尊重 Retry-After，降低并发或延后；
   - 认证/权限错误：不重试，明确要求用户操作；
   - 解析错误：一次结构修复；
   - 空结果：改写 query；
   - 永久抓取失败：保留失败证据并跳过；
   - 取消信号：尽快停止且状态一致。
6. 实现 BudgetManager：
   - 全局和每任务预算；
   - token 使用以 provider 返回 usage 为准；无 usage 时明确标记 estimated；
   - 搜索轮次和最大迭代数是硬上限；
   - warning/critical 阈值行为可配置；
   - 模型降级通过 provider 路由完成，不能在业务节点写死模型名；
   - critical 时停止未开始的非核心任务，并把信息缺口写入结果。
7. 实现动态 replan：
   - 只在明确 evidence gap 或新关键分支出现时追加；
   - max_replans 硬限制；
   - 新任务需去重并继承剩余预算；
   - 不允许追加与原问题无关的范围。
8. 接入 checkpoint：
   - 开发/测试可用内存实现；
   - 本地持久化使用项目当前兼容的 SQLite 方案；
   - thread/task id 语义清晰；
   - 恢复后不重复计费或重复写报告；
   - 为中断恢复写集成测试。
9. 增加上下文的第一层控制：在真正调用 LLM 前进行消息/输入长度检查。完整摘要压缩留到阶段 4。
10. 增加测试：循环终止、重复 query、预算耗尽、80%/95% 边界、429、认证错误、重试耗尽、replan 上限、取消、checkpoint 恢复。
11. 更新 docs/implementation-progress.md 和故障处理文档。

【明确禁止】
- 不用“LLM 说信息够了”作为唯一停止条件。
- 不无限重试，不递归无上限调用，不在测试中真实等待长退避时间。
- 不伪造精确 token；估算值必须标记 estimated。
- 不因单一来源的异常结论无限扩展研究范围。
- 不开始 CitationVerifier、完整 RAG、评测或前端。

【验收标准】
1. 所有循环都有 max_iterations、max_search_rounds、max_replans 或等效硬边界。
2. fake 场景能分别复现 sufficient、too_broad、too_narrow、off_track 和 conflicting_evidence。
3. 预算临界行为有边界测试；任务结束时能解释停止原因。
4. 认证错误不重试；临时错误按策略重试；测试使用 fake clock 或禁用真实等待。
5. 人为中断后可从 checkpoint 继续，已完成任务不重复执行。
6. 动态追加的任务与 evidence gap 有结构化关联。

【完成方式】
优先把循环控制、错误分类和预算策略写成可独立测试的纯逻辑，再接入图。每实现一种恢复行为，都必须添加失败测试。完成后运行一个包含“首次搜索失败→改写→成功→预算正常结束”的 fake 端到端案例，更新进度文档后停止，不得进入阶段 4。
```

---

# 阶段 4 Prompt：可信报告、引用验证、记忆与混合检索

```text
你现在负责 TechResearch Agent 的阶段 4。项目根目录是：
D:\Programs\Py_proj\DeepResearch Agent

本阶段把研究执行结果升级为“可追溯、可验证、可保存”的完整报告流程。核心是 CitationVerifier，不是单纯把 URL 附到报告末尾。

【进入阶段的前置门禁】
1. 阅读 docs/implementation-progress.md，确认阶段 3 的循环终止、预算和恢复测试通过。
2. 阅读 docs/技术设计文档.md 第 3.6、4.3、4.4、4.6、4.7、5.4、5.5、6.3、9.4、14、15 章。
3. 阅读 research/04_fact_audit/、research/05_memory_kg_feishu/ 中对应材料。
4. 阅读当前 Fact、Citation、Report、provider、fetcher 和 checkpoint 实现，先统一语义再扩展。
5. 核对当前 Chroma、embedding、rerank、LangGraph memory/store、飞书 SDK 的真实 API；不要照抄伪代码。

【本阶段目标】
A. 报告中的 claim、citation、原文证据形成可机读的映射。
B. CitationVerifier 重新获取来源并做 entailment/contradiction/neutral 判断。
C. 失败或存疑引用能触发一次受控修订，并保留完整审计记录。
D. 建立 MVP 分层记忆、上下文压缩和 BM25+向量混合检索。
E. 支持 Markdown、独立 HTML 和可选飞书导出。

【实施内容】
1. 定义稳定的 claim 模型，不要只依赖正则从任意句子猜 claim：
   - claim_id；
   - claim_text；
   - section_id；
   - citation_ids；
   - claim_type；
   - verification_status；
   - verification_note。
2. Writer 先生成结构化章节/claim，再渲染 Markdown。禁止 Writer 创建 citation_map 中不存在的编号。
3. 实现 CitationVerifier：
   - 校验引用编号和 URL/来源标识；
   - 重新 fetch 原始来源；
   - 获取与 claim 相关的上下文窗口；
   - 使用独立 verifier prompt/provider 做三分类；
   - 保存判定、理由、证据片段、模型/规则版本和时间；
   - 抓取失败与 neutral 分开处理；
   - contradiction/neutral 不得被计为通过。
4. 实现一次受控修订：
   - Writer 只能删除、不确定化或用已验证事实改写被标记 claim；
   - 最多重写一轮；
   - 重写后重新校验改动 claim；
   - 未解决问题进入“存疑引用/信息限制”章节。
5. 计算真实指标：Citation Precision、Claim Coverage、Hallucination/Contradiction Rate。分母为 0 时定义清晰并有测试。
6. 实现上下文压缩：
   - 裁剪和摘要在统一入口执行；
   - 结构化摘要必须保留 fact_id、citation_id、未解决问题和冲突；
   - 原始证据仍在外部存储可回查，不能因摘要而永久丢失 provenance；
   - token_counter 使用项目模型兼容的真实计数器或明确标记的估算器，不能用 len(messages) 冒充 token。
7. 实现 MVP 记忆：
   - 工作记忆：当前 ResearchState；
   - 会话 checkpoint：本地 SQLite 或当前兼容实现；
   - 长期 Store：只保存明确允许跨任务复用的偏好/结论摘要；
   - namespace 设计避免不同用户/项目数据串写；
   - 不在 MVP 接入 Mem0。
8. 实现混合检索：
   - 文档分块保留 source id、位置和哈希；
   - BM25 与向量检索；
   - 使用 RRF 或文档中确定的融合方式；
   - 去重和 top-k；
   - embedding provider 可 fake；
   - reranker 为可选开关，未配置不影响主流程。
9. 报告输出：
   - Markdown；
   - 独立 HTML，正确转义不可信内容，避免 XSS；
   - 飞书导出使用独立 adapter，只在用户配置后启用；
   - 导出失败不应破坏已生成的本地报告。
10. 测试至少覆盖：
   - 引用编号不存在；
   - 同一 claim 多引用；
   - entailment/contradiction/neutral；
   - refetch 失败；
   - 一轮修订；
   - 指标零分母；
   - 上下文压缩保留 provenance；
   - BM25/向量融合与去重；
   - HTML 转义；
   - 飞书未配置和失败隔离。
11. 生成一份 fake 端到端报告夹具，能展示通过、矛盾和存疑三类引用；明确标注为测试数据。
12. 更新 docs/implementation-progress.md、报告格式说明和手动配置清单。

【明确禁止】
- 不把 Worker 早期缓存的 snippet 直接当作 refetch 成功。
- 不把 NLI 模型输出当绝对真理；必须保留输入证据和判定元数据。
- 不实现多模型交叉验证、Mem0、LightRAG 或图库。
- 不把飞书导出设为主流程硬依赖。
- 不声称 Citation Precision 已达到某百分比，除非本阶段真实评测产生该数字；fake 数据不得用于宣传。

【验收标准】
1. 每个可验证 claim 都能追踪到 citation 和重新获取的证据片段。
2. 三分类与抓取失败状态不混淆，统计口径有测试。
3. 存疑/矛盾 claim 最多触发一次重写，不存在写作-验证无限循环。
4. 压缩后 citation_id/fact_id 和原始证据位置仍可回查。
5. 混合检索能用 deterministic fixture 验证融合排序。
6. Markdown 和 HTML 报告都可打开；HTML 对恶意标题/正文进行转义或安全清洗。
7. 无飞书配置时主流程仍完整成功。

【完成方式】
先定义 claim-citation-evidence 数据契约及状态迁移，再实现 Writer 和 Verifier；不要先写复杂正则。完成后运行包含三种判定状态的 fake 端到端测试，回读 Markdown/HTML，更新进度文档并停止，不得进入阶段 5。
```

---

# 阶段 5 Prompt：评测、基线、消融与失败分析

```text
你现在负责 TechResearch Agent 的阶段 5。项目根目录是：
D:\Programs\Py_proj\DeepResearch Agent

本阶段建立可复现评测体系。核心目标不是“做出好看的分数”，而是保证数据许可、配置、输入、过程、指标和输出都可复查。

【进入阶段的前置门禁】
1. 阅读 docs/implementation-progress.md，确认阶段 4 的报告与 CitationVerifier 测试通过。
2. 阅读 docs/技术设计文档.md 第 9、10、14、15、16 章。
3. 完整阅读 research/02_evaluation/ 与 research/04_fact_audit/deepresearch_bench_ii.md。
4. 核对 DeepResearch Bench II 当前官方仓库、数据格式、评测脚本和许可证；若无法联网，使用现有审计结论并把需再次核验项列为阻塞，不能猜。
5. 检查设计文档中所有“预期分数、成本、时间”表达，严禁把它们复制成真实结果。

【本阶段目标】
A. 建立版本化、可恢复、可比较的评测运行器。
B. 实现纯 LLM、朴素 RAG、完整系统三类基线/被测配置。
C. 实现关键指标和 4 组消融配置。
D. 先用小型 smoke 集验证，再支持 20～30 题子集；未经用户明确允许不启动高成本全量评测。
E. 输出原始结果、汇总、图表和失败案例，不伪造任何数字。

【实施内容】
1. 建立 evals/ 清晰结构：dataset adapter、runner、config、metrics、baselines、reports、plots、fixtures。
2. 数据集不得无说明直接复制进 Git：
   - 记录下载脚本或获取说明；
   - 保存版本/commit/hash；
   - 标注许可证和非商用限制；
   - 对不宜提交的大文件加入 .gitignore。
3. 实现可恢复评测运行器：
   - 每题独立保存状态；
   - 断点续跑；
   - 运行配置快照；
   - 模型/provider/提示词/代码版本元数据；
   - 随机种子；
   - 并发上限；
   - 成本上限；
   - 单题失败不丢失其他结果。
4. 实现配置：
   - llm_only：无检索；
   - naive_rag：单轮检索+直接生成，无反思/验证；
   - full：完整系统；
   - no_verifier；
   - no_reflection；
   - single_agent；
   - no_long_term_memory。
5. 确保每次消融只关闭目标组件，其他 provider、输入、预算和采样参数尽量相同。
6. 实现并测试指标：
   - 官方 rubric 通过率（按官方口径）；
   - Citation Precision；
   - Claim Coverage；
   - Faithfulness/上下文指标；
   - 检索轮次、token、延迟、成本；
   - 部分失败和零分母处理。
7. LLM judge：
   - prompt 和版本纳入配置；
   - judge 输入与输出落盘；
   - 支持少量人工校准样本；
   - 不在没有校准证据时宣称 judge 与人类高度一致。
8. 建立三级运行：
   - fixture 单元测试；
   - 2～3 题 smoke；
   - 20～30 题正式子集；
   全量 132 题必须是显式命令，并有预算确认参数。
9. 图表：
   - 指标分组柱状图；
   - 消融变化图；
   - 成本-质量帕累托图；
   - 图表数据必须直接读取原始结果，不手填。
10. 失败分析：自动按检索失败、推理失败、生成幻觉、时效失败、工具失败分类，再人工复核最多 10 个代表性案例；不能编造案例。
11. 输出 evals/README.md、真实报告模板和运行清单。没有真实 live 结果时，明确写“仅通过 fixture/smoke 验证”，不得创建虚假的正式评测报告。
12. 更新 docs/implementation-progress.md。

【明确禁止】
- 不自动启动昂贵的 132 题全量运行。
- 不把 fake/smoke 结果写进 README 的项目成绩。
- 不把设计文档中的 92%、78%、40%、¥150 等示例/预估当成测量结果。
- 不修改官方数据集答案或 rubric 以提高分数。
- 不让不同系统配置使用不同题目子集后直接横向比较。
- 不把 API 失败样本静默排除出分母；排除规则必须显式。

【验收标准】
1. fixture 测试可在无外部 Key 环境完整通过。
2. runner 中断后可续跑，已完成题不重复计费。
3. 每条结果可追溯到题目、配置、代码版本、模型和原始输出。
4. 各消融配置只改变目标开关，并有配置快照证明。
5. 汇总和图表由原始结果程序化生成，重新运行可复现。
6. 高成本命令具备明确预算/题量保护，默认只跑 smoke。
7. 报告清楚区分 fixture、smoke、正式子集和全量结果。

【完成方式】
先用最小 fixture 建立可靠管线，再尝试真实 smoke；不要一上来跑正式子集。若需要真实付费调用但用户未提供 Key 或预算许可，完成所有离线部分并列出精确命令和预计调用范围，不能伪称已运行。更新进度文档后停止，不得进入阶段 6。
```

---

# 阶段 6 Prompt：FastAPI、SSE、React Web 应用与 CLI

```text
你现在负责 TechResearch Agent 的阶段 6。项目根目录是：
D:\Programs\Py_proj\DeepResearch Agent

本阶段把已验证的核心服务接入 API、SSE、React 四页应用和 CLI。不要在前端重新实现后端业务逻辑。

【进入阶段的前置门禁】
1. 阅读 docs/implementation-progress.md，确认核心研究服务、引用验证和评测 smoke 均有可运行入口。
2. 阅读 docs/技术设计文档.md 第 6.5、7、8、13、14、15、16 章。
3. 阅读现有 FastAPI、CLI、事件模型、任务状态、报告输出和 web 工程源码。
4. 核对当前 FastAPI/Pydantic/React/Vite/TypeScript/Tailwind/shadcn 版本与真实 API。
5. 如果前端模板与设计文档技术栈不同，不擅自推倒重建；先评估最小兼容方案。

【本阶段目标】
A. 提供稳定 REST API 和单向 SSE 进度流。
B. React 实现创建页、进度页、报告页、历史页。
C. 实时展示任务树、Worker 状态、工具活动、反思和引用验证，但不泄露敏感正文或内部思考。
D. CLI 与 Web 共用同一 application service。
E. 浏览器刷新、SSE 重连、任务失败和取消都有可理解行为。

【实施内容】
1. 设计并冻结 API schema：
   - POST /api/research；
   - GET /api/research/{task_id}；
   - GET /api/research/{task_id}/report；
   - GET /api/research/{task_id}/stream；
   - GET /api/tasks；
   - POST /api/research/{task_id}/export/feishu；
   - POST /api/research/{task_id}/cancel（若核心层已支持取消）；
   - GET /api/health。
2. API 不得返回任意内部对象；使用显式 Pydantic response model。
3. 任务执行：
   - 本阶段可使用进程内后台任务，明确其限制；
   - 不引入 Celery/Redis；
   - 进程重启后的可恢复性以现有 checkpoint 为准；
   - 同一 task_id 幂等行为清晰。
4. SSE 事件 envelope 至少包含：event_id、event_type、task_id、timestamp、stage、data、schema_version。
5. SSE 处理：
   - 心跳；
   - 断线重连和 Last-Event-ID 或等效恢复；
   - 完成/失败终止事件；
   - 慢客户端与内存增长控制；
   - 序列化失败隔离；
   - 不推送 API Key、完整 prompt、模型内部 reasoning 或超大原文。
6. 统一错误响应；区分参数错误、配置缺失、外部依赖错误、预算终止、任务不存在和内部错误。
7. React 四页：
   - HomePage：问题、背景、深度、数据源；
   - ProgressPage：任务树、Worker 卡片、阶段进度、可折叠事件流；
   - ReportPage：安全 Markdown、引用 tooltip/跳转、验证状态、导出；
   - HistoryPage：历史任务、状态、创建时间、恢复入口。
8. 安全渲染：
   - Markdown 禁止任意 HTML 或进行严格清洗；
   - 外链使用安全属性；
   - 不用 dangerouslySetInnerHTML 渲染不可信内容，除非经过明确 sanitization 且有测试。
9. 前端状态：
   - 服务端状态使用现有约定（如 React Query）；
   - UI 本地状态使用轻量方案；
   - SSE 增量事件去重；
   - 刷新后从 REST 当前快照恢复，再继续订阅。
10. CLI 支持创建任务、查看状态、流式观察和导出本地报告；与 API 共用 schema/service。
11. 测试：
   - FastAPI 路由与错误契约；
   - SSE 顺序、重连、完成、失败和重复事件；
   - React 组件与关键交互；
   - TypeScript 类型检查；
   - 前端生产构建；
   - fake provider 的前后端 smoke/E2E。
12. 更新 API 文档、前端启动文档和 docs/implementation-progress.md。

【明确禁止】
- 不引入生产级任务队列、Redis、多用户认证或云部署。
- 不把 LangGraph state 原样序列化给浏览器。
- 不把 chain-of-thought/reasoning_content 展示给用户。
- 不让前端直接持有任何 LLM、搜索或飞书密钥。
- 不用轮询冒充 SSE；可以用 REST 获取初始快照，但增量进度使用 SSE。

【验收标准】
1. fake 模式下，用户能在浏览器创建任务，看到进度，打开最终报告。
2. 页面刷新后任务状态不丢，SSE 重连不会重复渲染事件。
3. 失败任务显示结构化原因和可执行建议，页面不白屏。
4. 引用可点击回溯，矛盾/存疑状态清晰且不误标为通过。
5. API schema、前端类型和真实响应一致。
6. 后端测试、前端测试、类型检查、生产构建和 fake E2E 全部通过。
7. 未配置飞书时只禁用飞书导出，不影响其他功能。

【完成方式】
先冻结事件和 API schema，再做后端路由，最后接前端。使用 fake provider 完成浏览器真实入口验证，并保存必要的测试证据；不要用静态假页面代替联调。更新进度文档后停止，不得进入阶段 7。
```

---

# 阶段 7 Prompt：质量收口、性能、文档、开源与面试准备

```text
你现在负责 TechResearch Agent 的阶段 7。项目根目录是：
D:\Programs\Py_proj\DeepResearch Agent

这是 MVP 收口阶段。目标是把已经实现的系统变成可复现、可公开、可演示、能经受面试追问的项目，而不是继续堆新功能。

【进入阶段的前置门禁】
1. 完整阅读 docs/implementation-progress.md 和所有未解决项。
2. 阅读 docs/技术设计文档.md 第 1、2、9、10、12、14、15、16、17、18 章。
3. 检查整个仓库、Git 状态、测试、构建、依赖、许可证、密钥、生成产物和文档。
4. 对照真实代码修正文档。设计文档与实现不一致时，不能把未实现功能写成已完成。
5. 阅读真实评测原始结果。没有正式结果时，不得保留任何虚构百分比或成本数字。

【本阶段目标】
A. 修复阻塞发布的正确性、稳定性、安全和可复现性问题。
B. 完成全链路测试、性能基线和关键失败场景验证。
C. 完成 README、API、部署/开发、评测和架构文档。
D. 清理所有虚假成绩、过时结论、秘密和不可复现步骤。
E. 准备基于真实实现与真实数据的 Demo 和面试材料。

【实施内容】
1. 建立发布前检查清单，按严重度处理：
   - P0：数据错误、密钥泄露、报告引用错配、状态死循环、无法启动；
   - P1：恢复失败、预算越界、SSE 丢事件、前后端契约不一致；
   - P2：性能、可维护性、文档和体验。
2. 执行全量质量检查：
   - 后端全部单元/集成测试；
   - lint/format/type check；
   - 前端测试、类型检查、生产构建；
   - fake provider 全链路 E2E；
   - 可用时执行小规模 live smoke，但不得默认付费；
   - 评测 fixture/smoke 回归。
3. 增加关键韧性测试：
   - 搜索超时；
   - LLM 429；
   - Worker 部分失败；
   - checkpoint 恢复；
   - 预算临界；
   - 引用 contradiction/neutral；
   - SSE 断开重连；
   - 前端刷新；
   - 飞书导出失败。
4. 做真实性能基线：
   - 端到端耗时；
   - 各阶段耗时；
   - LLM/搜索调用数；
   - token usage（真实或明确 estimated）；
   - 内存；
   - 并发 1/2/3 的变化。
   只优化有测量证据的瓶颈。
5. 可以做的有限优化：
   - 消除重复 fetch；
   - 批量或并发安全的独立调用；
   - 减少重复序列化和不必要 LLM 调用；
   - 合理缓存单次任务内的纯结果。
   不引入 Redis 或复杂生产基础设施。
6. README 必须包含：
   - 项目定位；
   - 真实架构图；
   - 核心亮点及代码位置；
   - 快速开始；
   - fake 模式；
   - live 配置；
   - Web/CLI 使用；
   - 测试；
   - 评测方法和真实结果；
   - 已知限制；
   - 路线图；
   - 许可证与第三方数据许可。
7. 修订 docs/技术设计文档.md：
   - 明确“设计”“已实现”“待实现”；
   - 删除或标注未经实验验证的面试示例数字；
   - 模型/API 名称与当前配置一致；
   - 更新目录结构、API、状态和运行命令；
   - 保留证据边界，不作未审计开源项目的绝对结论。
8. 生成 Demo 指南：
   - 5 分钟流程；
   - 使用固定且合法的演示问题；
   - 预期界面步骤；
   - live API 失败时的可诚实 fallback；
   - 禁止伪造联网结果。
9. 生成面试材料：
   - 30 秒项目介绍；
   - 3 分钟架构讲解；
   - CitationVerifier、调度、评测三个深挖主题；
   - 常见追问；
   - 所有量化表述必须引用真实评测文件或明确写“尚未测量”。
10. 开源检查：
   - 选择并核对项目 LICENSE；
   - 第三方数据和代码许可证；
   - .gitignore；
   - secret scan；
   - 大文件；
   - 个人路径/内部链接/私有信息；
   - 依赖锁定和安装复现；
   - 贡献指南可选。
11. 创建 docs/release-checklist.md 和最终版 docs/implementation-progress.md。
12. 未经用户明确授权，不 push、不发布 release、不部署线上服务。

【明确禁止】
- 不新增 Mem0、LightRAG、MCP、GitHub 源码研究、Celery、Redis、多用户和 Docker 等范围外功能。
- 不为“看起来完成”而删除失败测试或降低断言。
- 不写虚构评测结果、虚构线上用户量、虚构性能提升。
- 不把 API Key、个人路径、内部公司链接或私有数据提交到公开材料。
- 不使用 git reset --hard、强制 push 或删除用户未提交工作。

【最终验收标准】
1. 按 README 从干净环境可安装、启动并运行 fake demo。
2. 全量自动化检查通过；未通过项有明确根因、影响和后续动作。
3. 核心研究流程无死循环，所有预算和重试有硬上限。
4. 报告 claim-citation-evidence 可追溯，验证状态口径一致。
5. Web、CLI 和 API 使用同一核心服务，结果一致。
6. 评测结果可从原始文件重新汇总，图表可重建。
7. 仓库不含真实密钥、内部敏感信息或虚假项目成绩。
8. 文档对“已实现/未实现/未来规划”的表述与源码一致。
9. release checklist 每一项有证据或明确未完成标记。

【完成方式】
先做发布审计并按 P0/P1/P2 排序，再修复。优先保证正确性和可复现性，不继续扩功能。最终给出可直接执行的启动、测试和 Demo 命令，列出真实评测结果文件位置；若没有正式结果，明确说明。更新最终进度文档后停止，等待用户决定是否公开仓库或继续进阶能力。
```

---

## 3. 可选 Prompt：阶段失败后的修复专用

当某阶段验收失败时，不要直接重跑整个阶段，可以使用下面的修复 Prompt。

```text
你现在只负责修复 TechResearch Agent 当前阶段的验收失败，不扩展功能。

项目根目录：D:\Programs\Py_proj\DeepResearch Agent

请先读取：
1. docs/implementation-progress.md；
2. 当前阶段 Prompt；
3. 失败的测试输出、日志和相关源码；
4. git diff 与未提交修改。

要求：
- 先稳定复现失败，记录最小复现命令。
- 找到根因，不通过删除测试、降低断言、吞异常、硬编码结果或增加无上限重试绕过。
- 修改范围保持最小；任何公共契约变化都要同步测试和文档。
- 为根因补充回归测试。
- 依次执行最小相关测试、受影响测试集、全量检查和真实入口 smoke test。
- 更新 docs/implementation-progress.md，说明失败原因、修复内容和验证证据。
- 最终只报告修复结果和是否重新满足当前阶段门禁，不开始下一阶段。
```

---

## 4. 可选 Prompt：每阶段代码审查

建议在每个阶段完成后，让另一个 AI 或新的上下文执行一次只读审查。

```text
请对 TechResearch Agent 当前阶段做只读代码审查，不修改文件。

项目根目录：D:\Programs\Py_proj\DeepResearch Agent

审查依据：
- docs/技术设计文档.md；
- docs/implementation-progress.md；
- 当前阶段 Prompt 的范围和验收标准；
- 当前源码、测试、配置和实际 git diff。

重点寻找：
1. 与技术方案不一致且未记录的实现；
2. 只在 happy path 工作的逻辑；
3. 并发竞态、状态覆盖、死循环和重复副作用；
4. 重试、预算、取消和恢复边界；
5. claim/citation/provenance 丢失；
6. 真实 API schema 被猜测；
7. 测试通过但没有验证真实入口；
8. 密钥、隐私、XSS、SSRF、路径遍历和不安全反序列化；
9. 假数据或预估数据被当成真实成绩；
10. 阶段外功能导致的复杂度膨胀。

输出格式：
- 先给审查结论：通过 / 有条件通过 / 不通过；
- 按 P0、P1、P2 列问题；
- 每个问题提供文件路径、代码位置、可复现方式、影响和最小修复建议；
- 列出已经检查且未发现问题的范围；
- 不臆测未读取的实现，不输出泛泛建议。
```

---

## 5. 阶段门禁总表

| 阶段结束 | 必须通过的最小门禁 | 不通过时的动作 |
|---|---|---|
| 阶段 0 | 无 Key 测试通过；health/CLI/前端壳可运行；无秘密 | 只修工程基线 |
| 阶段 1 | fake 单流程报告可生成；事实和引用可追溯 | 修工具契约和纵向切片 |
| 阶段 2 | 并行真实发生；依赖正确；聚合确定 | 修状态/reducer/调度 |
| 阶段 3 | 循环有界；预算生效；可恢复；错误分级 | 修控制流和持久化 |
| 阶段 4 | claim-citation-evidence 闭环；三分类可靠落盘 | 修可信报告链路 |
| 阶段 5 | 评测可恢复可复现；结果不伪造；默认低成本 | 修评测管线和口径 |
| 阶段 6 | 浏览器 fake E2E；SSE 重连；安全渲染 | 修 API/事件/前端契约 |
| 阶段 7 | 干净环境可复现；全量检查通过；文档与代码一致 | 不发布，继续修复 |

---

## 6. 明确延后到 MVP 之后的内容

以下内容不应被任何阶段 AI 顺手加入：

- Mem0 自动长期记忆；
- LightRAG 或其他知识图谱数据库；
- GitHub 源码级研究；
- MCP Server；
- 多模型交叉引用验证；
- Celery/RQ 任务队列；
- Redis 缓存；
- 多用户、登录、权限和数据隔离；
- Docker 和云部署；
- 全量 DeepResearch Bench II 高成本评测；
- 商业化能力。

只有在阶段 7 通过、用户重新评审并明确选择后，才能为这些内容另行制定 Prompt。

---

## 7. 方案来源映射

本文阶段划分直接映射 `docs/技术设计文档.md`：

- 阶段 0～1：第 11、13、14 章的基础框架与第 5 章工具层；
- 阶段 2：第 3、4.1、4.2、6.2 章；
- 阶段 3：第 3.6、4.5、6.4、10 章；
- 阶段 4：第 4.3、4.4、4.6、5.4、5.5、6.3 章；
- 阶段 5：第 9、10 章；
- 阶段 6：第 7、8 章；
- 阶段 7：第 12、15、16、17、18 章及 12 周路线图收口要求。

本文对原 12 周路线做了两个执行层面的调整：

1. 把“基础框架”和“第一条可运行研究链路”分开，避免初始阶段同时创建大量空模块。
2. 把“可信报告闭环”放在评测之前，确保评测测量的是稳定的数据契约，而不是不断变化的中间实现。

这两项调整不改变技术方案，只降低分阶段交给 AI 实施时的返工风险。
