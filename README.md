# TechResearch Agent

A minimal multi-agent research assistant scaffold. It takes a technical
question, runs a Supervisor-Worker graph that searches, fetches, extracts
facts, and writes a Markdown report — then re-fetches every cited source
and runs a 3-way entailment check.

> **Status: MVP with explicit stubs.** The offline / fake-provider path is
> fully tested. Several "real" adapters (NLI classifier, embedding,
> Feishu export, LLM judge) are **stubs**, not production integrations.
> Live Tavily/Qwen calls are wired but not exercised against paid APIs
> in this repo.

## What is actually implemented (tested)

| Layer | Code | Status |
|---|---|---|
| State + reducers (typed, concurrency-safe) | `src/graph/state.py` | real |
| Self-rolled Supervisor routing (pure function) | `src/agents/supervisor.py` | real |
| Planner (heuristic, deterministic) | `src/agents/planner.py` | real |
| Worker loop (search → fetch → extract → reflect) | `src/agents/worker.py` | real |
| Reflection over observable signals | `src/agents/reflection.py` | real |
| Error classification + backoff | `src/agents/errors.py` | real |
| Budget (token/rounds/replans, warning/critical) | `src/agents/budget.py` | real |
| CitationVerifier refetch loop | `src/service/verifier.py` | real |
| Verified report (Markdown + escaped HTML) | `src/service/verified_report.py` | real |
| FastAPI + SSE + React UI | `src/api/`, `web/` | real |
| Eval harness (offline, resumable) | `evals/` | real (fixture only) |
| QwenProvider (httpx OpenAI-compatible) | `src/core/providers/qwen.py` | real, mock-tested |
| LangSmith tracing env adapter | `src/core/tracing.py` | real, env-only |
| Prompt Hub (local manifest + LangSmith sync) | `src/core/prompts/` | real, mock-tested + live smoke |

## What is a stub (do not claim as production)

- **NLI classifier** in CitationVerifier: keyword stub, not a trained model.
- **Embedding provider**: fake hash-based; no real vector DB.
- **FeishuExporter**: returns `(exported=False, reason="not_configured")`.
- **LLM judge** in evals: `KeywordJudge`, not a real LLM judge.
- **SSE**: 100ms polling of in-memory store, not production push.
- **Markdown rendering**: `<pre>` in browser, no rich markdown renderer.
- **Task persistence**: in-memory only; restart loses running tasks.

## Quick start (fake mode, no API keys)

Requires Python 3.11+ (tested on 3.14.7) and Node 18+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest        # 125 tests, offline
.\.venv\Scripts\ruff.exe check src tests evals
.\.venv\Scripts\ruff.exe format --check src tests evals
.\.venv\Scripts\mypy.exe

cd web
npm install
npm test           # 1 component test
npm run build
```

Run the API:

```powershell
.\.venv\Scripts\uvicorn.exe api.main:app --reload --port 8000
# another terminal:
cd web; npm run dev
```

## Live mode (optional, not exercised in this repo)

Copy `.env.example` to `.env` and fill in:

- `TAVILY_API_KEY` — web search
- `DASHSCOPE_API_KEY` — Qwen (DashScope) LLM
- `LANGCHAIN_API_KEY` — optional LangSmith tracing

See `docs/manual-setup.md`. Without these keys the system runs entirely on
fake providers and all tests still pass.

## Tests

```
.\.venv\Scripts\python.exe -m pytest    # 125 tests, no network, no paid calls
cd web; npm test                        # 1 component test
```

## Evaluation

See `evals/README.md`. Currently **only fixture/smoke validated**. No real
benchmark numbers are claimed in this repo.

## Known limitations (honest list)

- In-process task store; restart loses running tasks.
- SSE uses polling, not a production push layer.
- NLI / embedding / token counter are heuristic stubs.
- No auth, no multi-user, no Docker, no CI, no deployment config.
- No live benchmark results; no fake percentages claimed.

## License

MIT. See `LICENSE`. Research data under `research/` is third-party reading
material; the synthetic fixture dataset in `evals/` is original.

## Docs

- `docs/implementation-progress.md` — what is actually built, by phase
- `docs/release-checklist.md` — pre-release audit findings
- `docs/demo-guide.md` — 5-minute demo (fake mode)
- `docs/interview.md` — talking points
