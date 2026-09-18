# API、SSE 与前端

## FastAPI 入口

入口：`src/api/main.py`，应用对象为 `api.main:app`。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务、Provider 和部分非敏感配置状态 |
| POST | `/api/research` | 创建研究任务 |
| GET | `/api/research/{task_id}` | 查询任务详情 |
| GET | `/api/research/{task_id}/report` | 获取 Markdown 与 HTML 报告 |
| GET | `/api/research/{task_id}/stream` | 读取 SSE 事件流 |
| GET | `/api/tasks` | 查询最近任务 |
| POST | `/api/research/{task_id}/cancel` | 标记任务取消 |
| POST | `/api/research/{task_id}/export/feishu` | 导出飞书文档 |

## 请求与响应约定

创建任务请求：

```json
{
  "query": "LangGraph vs LlamaIndex",
  "user_context": "关注生产可维护性",
  "research_depth": "standard"
}
```

API 使用专门的 Pydantic 模型，不直接返回 LangGraph state。所有响应会带 `X-Request-ID`，可由调用方传入，也可由服务生成。

## 任务状态

```text
queued → running → completed
                 ↘ failed
                 ↘ cancelled
```

如果启用 `TASK_STORE_PATH`，任务可保存为 JSON。服务启动后发现旧进程遗留的 queued/running 任务时，会改为 failed，并写入 `interrupted_by_service_restart`。

注意：TaskStore 的持久化目前是同步文件 I/O，保存异常会被吞掉；这属于已知生产化缺口。

## SSE 协议

事件 envelope：

```json
{
  "event_id": "...",
  "event_type": "stage",
  "task_id": "...",
  "timestamp": 0,
  "stage": "planner_start",
  "data": {},
  "schema_version": "1.0"
}
```

典型 stage：

```text
queued
running
planner_start
planner_done
worker_done
verify_start
verify_done
write_done
done / failed / cancelled
```

客户端可通过 `Last-Event-ID` 请求重放后续事件。服务端当前用 100ms 轮询 TaskRecord 事件列表，最长约 60 秒，不是生产级消息总线。

## React 前端

入口：`web/src/App.tsx`。

页面使用 hash 路由：

- `#/`：创建任务。
- `#/progress/{task_id}`：查看事件、Worker 卡片和整体进度。
- `#/report/{task_id}`：查看报告、下载 Markdown、导出飞书。
- `#/history`：任务历史。

### 安全 Markdown

`SafeMarkdown` 不使用 `dangerouslySetInnerHTML`，而是将字符串作为 React child 渲染。当前支持：

- 一级、二级标题。
- 无序列表。
- 段落。
- `[c1]`、`[c_task_1_1]` 等引用锚点。

它不是完整 Markdown 解析器；代码块、表格、强调、嵌套列表等不会完整渲染。

## 本地启动

后端：

```powershell
.\.venv\Scripts\uvicorn.exe api.main:app --reload --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd web
npm install
npm run dev
```

Vite 会把 `/api/*` 代理到 `http://localhost:8000`。

## 修改接口时必须同步

1. `src/api/main.py` 的 Pydantic 模型和路由。
2. `web/src/App.tsx` 的 TypeScript 类型和调用代码。
3. `tests/unit/test_api_routes.py` 及相关接受测试。
4. `web/src/*.test.tsx` 和必要的 Playwright 测试。
5. 本文档和根 README 的接口说明。
