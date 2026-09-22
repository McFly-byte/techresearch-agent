# TechResearch Agent

一个面向技术调研的多智能体研究应用。项目使用 LangGraph 组织 Planner、Supervisor 和 Worker，执行搜索、网页抓取、事实提取与反思，并在生成报告后重新抓取引用来源进行验证。

> 当前定位：**研究型 MVP**。离线 fake 模式可直接运行和测试；真实联网模式已有 Tavily、Qwen/DeepSeek、LangSmith 与飞书适配，但仍缺少认证、分布式任务队列、生产级 SSE 和部署配置。

## 核心能力

- Supervisor-Worker 多任务编排，支持依赖、并发上限和失败传播。
- 搜索 → 抓取 → 事实提取 → 反思的有界 Worker 循环。
- 全局/单任务 token 预算、搜索轮次和重规划硬上限。
- Fact → Claim → Citation → Evidence 的完整追溯链。
- 引用重新抓取与三分类验证：支持、矛盾、证据不足。
- fake/live 严格分离：fake 强制零网络，live 缺配置时明确失败。
- FastAPI + SSE + React Web 界面。
- 可续跑评测、DeepResearch Bench II 适配和 rubric judge。
- 本地 Prompt 清单与 LangSmith Prompt 同步。

## 项目结构

```text
src/
  agents/            规划、调度、Worker、反思、预算、错误处理
  api/               FastAPI、任务存储和后台执行器
  core/              配置、Provider、Prompt、日志与追踪
  domain/            事实、引用、声明和验证模型
  graph/             LangGraph 状态、Reducer 和图构建
  service/           提取、验证、报告、检索、记忆和导出
  tools/             搜索、抓取和工具协议
web/                 React + TypeScript 前端
evals/               评测适配、Runner、Judge、图表和结果
tests/               后端测试
docs/                设计稿、实施记录和历史手册
knowledge-base/      面向开发者和后续 Agent 的中文知识库
research/            项目调研与审计材料
```

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+
- Git

### 安装后端

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 配置

```powershell
Copy-Item .env.example .env
```

默认不需要填写任何密钥，系统会使用离线 fake 模式。真实模式至少需要：

```dotenv
DASHSCOPE_API_KEY=你的密钥
TAVILY_API_KEY=你的密钥
# 可选：多个 Tavily Key 用逗号分隔，支持轮询和自动故障切换
TAVILY_API_KEYS=Key1,Key2,Key3
LLM_PROVIDER=qwen
```

不要提交 `.env`。

### 启动后端

```powershell
.\.venv\Scripts\uvicorn.exe api.main:app --reload --host 127.0.0.1 --port 8000
```

健康检查：<http://127.0.0.1:8000/api/health>

### 启动前端

```powershell
cd web
npm install
npm run dev
```

访问 <http://localhost:5173>。Vite 会把 `/api/*` 代理到本地 FastAPI。

## CLI

安装项目后可使用 `tra`：

```powershell
tra doctor
tra config
tra research "LangGraph vs LlamaIndex"
tra research "RAG survey" --live --papers
tra run "LangGraph vs LlamaIndex" --depth standard
tra prompts list
tra prompts validate
```

说明：

- `tra research` 是较早的简单线性管线。
- `tra run` 与 HTTP API 共用 `ResearchRunner`，默认 fake 模式。
- 当前 HTTP/前端创建任务接口未暴露 live 模式选择，默认运行 fake。

## fake 与 live 模式

| 模式 | 搜索/抓取/模型 | 网络 | 用途 |
|---|---|---|---|
| fake | 固定 SearchResult、FakeFetcher、FakeLLM | 强制关闭 | 开发、测试、演示 |
| live | Tavily、真实网页、Qwen 或 DeepSeek | 开启 | 真实调研与正式评测 |

fake 报告会显示测试数据警告。不得将 fake 结果作为真实研究结论或 benchmark 成绩。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/research` | 创建研究任务 |
| GET | `/api/research/{task_id}` | 查询任务状态 |
| GET | `/api/research/{task_id}/report` | 获取 Markdown/HTML 报告 |
| GET | `/api/research/{task_id}/stream` | SSE 事件流 |
| GET | `/api/tasks` | 最近任务 |
| POST | `/api/research/{task_id}/cancel` | 请求取消 |
| POST | `/api/research/{task_id}/export/feishu` | 导出飞书文档 |

接口和前端细节见 [knowledge-base/06-API与前端.md](knowledge-base/06-API与前端.md)。

## 测试与质量检查

后端：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check src tests evals
.\.venv\Scripts\ruff.exe format --check src tests evals
.\.venv\Scripts\mypy.exe
```

前端：

```powershell
cd web
npm test
npm run typecheck
npm run build
```

浏览器 E2E：

```powershell
cd web
npx playwright install chromium
npm run test:e2e
```

## 评测

离线 smoke：

```powershell
.\.venv\Scripts\tra-eval.exe run --mode fake --judge keyword
```

真实数据子集：

```powershell
.\.venv\Scripts\tra-eval.exe run `
  --dataset 路径\dataset.jsonl `
  --mode live `
  --judge llm `
  --limit 3 `
  --output-dir evals\runs\pilot
```

不指定 `--limit` 且不提供 `--confirm-full` 时，评测只跑 3 题 smoke。第三方 benchmark 数据集不纳入仓库。

## 知识库

后续开发者和 Agent 应从 [knowledge-base/README.md](knowledge-base/README.md) 开始阅读。知识库覆盖：

- 当前架构与执行链路。
- 数据模型和关键不变量。
- 配置、运行模式、API 和前端。
- 引用验证、可信报告和评测体系。
- 测试门禁、扩展方法、已知限制和接手流程。

历史设计与实施记录仍保留在 `docs/`，但判断当前行为时以源码、测试和 `knowledge-base/` 为准。

## 当前已知限制

- API/前端尚未暴露 live 模式选择。
- API 的取消状态尚未真正连接到运行中 Worker 的取消事件。
- FastAPI 创建 `FeishuExporter` 时尚未注入 Settings，因此默认导出为未配置。
- TaskStore 是单进程模型，JSON 持久化字段不完整且写入错误会被吞掉。
- SSE 使用内存事件列表轮询，不是生产级事件系统。
- 无认证、多用户隔离、CI、Docker 和部署配置。
- 默认 Planner 仍是启发式；混合检索、长期记忆和 arXiv 尚未接入完整主链路。

详情见 [knowledge-base/11-已知限制与路线图.md](knowledge-base/11-已知限制与路线图.md)。

## 安全约定

- 不提交 `.env`、密钥、令牌、Cookie 或私有数据。
- 模型报告不能通过 `dangerouslySetInnerHTML` 直接渲染。
- 只有 `http://` 和 `https://` 引用可生成可点击链接。
- 长期记忆默认关闭，只有显式同意且在 allow-list 中的数据可持久化。
- 新的网络抓取功能必须考虑 SSRF、大小限制、超时和重定向风险。

## License

项目代码采用 MIT License，见 [LICENSE](LICENSE)。`research/` 和本地评测数据可能来自第三方，其许可证与使用边界需分别核对。
