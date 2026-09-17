# 开发手册（Windows）

> 阶段 0 基线。本手册描述在新机器上从零安装、运行、测试本项目的步骤。
> 所有命令均在 **PowerShell 5.1+** 下验证过（Windows 11）。

## 0. 前置要求

| 工具 | 最低版本 | 验证命令 |
|---|---|---|
| Python | 3.11+（开发机当前为 3.14.7，已验证可装） | `python --version` |
| Node.js | 18+（开发机 v22.23.2） | `node --version` |
| npm | 9+（开发机 10.9.8） | `npm --version` |
| Git | 任意近期版本 | `git --version` |

> 注意：`requires-python = ">=3.11"`，未设上限。阶段 0 的运行依赖
> （pydantic / pydantic-settings / fastapi / uvicorn / click）在 Python 3.14.7 上
> 已实际安装成功。LangGraph / LangChain 尚未引入，留到阶段 1 再验证兼容性。

## 1. 克隆与进入目录

```powershell
cd "D:\Programs\Py_proj\DeepResearch Agent"
```

（阶段 0 开始时本仓库还不是 Git 仓库；如需版本管理可自行 `git init`。）

## 2. 安装后端（Python）

```powershell
# 创建并激活虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 升级 pip 并以 editable 模式安装项目（含 dev 依赖）
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

安装完成后会得到两个命令：

- `pytest` / `ruff` / `mypy`（通过 venv 的 Scripts 目录）
- `tra`（项目自带 CLI，入口在 `src/cli/doctor.py`）

## 3. 配置环境变量

```powershell
copy .env.example .env
```

**阶段 0 不需要填任何真实 Key**。所有外部 Key 都是可选的：
- 不填 `DASHSCOPE_API_KEY` → 默认使用 `FakeLLM`，测试与本地启动完全离线。
- 想接真实 LLM 时，再按 `docs/manual-setup.md` 填写。

## 4. 运行测试 / lint / 类型检查

```powershell
# 单元测试（39 个用例，无外部依赖）
pytest

# Lint
ruff check src tests

# 类型检查
mypy
```

三条命令在阶段 1 都应退出码 0。

## 5. 启动后端

```powershell
uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

访问 <http://127.0.0.1:8000/api/health>，应返回稳定 JSON：

```json
{
  "status": "ok",
  "phase": 0,
  "service": "techresearch-agent",
  "llm": { "provider": "fake", "model": "fake-1", "configured": "True" }
}
```

## 6. 启动前端

```powershell
cd web
npm install
npm run dev        # http://localhost:5173
```

生产构建 / 类型检查：

```powershell
npm run typecheck
npm run build      # 产物在 web/dist/
```

Vite dev server 已配置把 `/api/*` 代理到 `http://localhost:8000`。

## 7. CLI

```powershell
tra doctor      # 环境/配置自检，区分"本地可运行"与"缺少可选外部 Key"
tra config      # 打印当前 Settings（所有 SecretStr 字段自动打码为 ***REDACTED***）
```

`tra doctor` 的退出码：
- `0`：本地可运行（Python 版本达标、provider 可实例化）。
- `1`：本地不可运行（Python < 3.11）。

缺少外部 API Key **不会**导致 doctor 失败，它们只会被列为 `[MISS]` 项。

### 7.1 跑一个简单的调研切片（阶段 1）

```powershell
# Fake 模式（默认，无需任何 Key，离线）：
tra research "LangGraph vs LlamaIndex"

# 联网模式（需要 .env 里有 TAVILY_API_KEY）：
tra research "LangGraph vs LlamaIndex" --live

# 同时查 arXiv：
tra research "RAG survey" --live --papers
```

不带 `--live` 时，CLI 用内置的 fake 搜索/fetcher，输出一段固定的 Markdown
报告（见 `tests/fixtures/fake_e2e_report.md`），用来验证整条流水线通了。
带 `--live` 但没填 `TAVILY_API_KEY` 时，CLI 会直接报可操作的错误并退出。

## 8. 常见问题

- **`Activate.ps1` 被执行策略拦截**：以当前用户身份一次性放开
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。
- **`pip` 装到了全局而非 venv**：确认 `where python` 指向 `.venv\Scripts\python.exe`，
  或显式使用 `.\.venv\Scripts\python.exe -m pip ...`。
- **改了 Settings 但测试没生效**：`get_settings()` 是 `lru_cache`，测试里用
  `get_settings.cache_clear()` 复位（测试 fixture 已自动处理）。
