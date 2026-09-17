# Release Checklist (Phase 7, final correction)

> Items without evidence are marked NOT VERIFIED / OPEN. "Accepted limitation"
> is not used as a status.

## P0 (must fix before public release)

| # | Item | Status | Evidence |
|---|---|---|---|
| P0-1 | No real API keys in repo | PASS | secret scan found only redacted `sk-xxxx` in research/ docs |
| P0-2 | All tests pass offline | PASS | `pytest` → 457 passed, 0 warnings |
| P0-3 | No infinite loops | PASS | all loops have max_iterations / max_search_rounds / max_replans caps |
| P0-4 | App starts | PASS | `uvicorn api.main:app` imports cleanly; /api/health returns 200 |
| P0-5 | Report citation ids match citations | PASS | `_CitationGuard` rejects unknown ids; verifier re-fetches; rewrite re-verifies |
| P0-6 | Frontend builds + tests | PASS | `npm run build` OK; `npm test` → 3 passed |
| P0-7 | ruff format --check | PASS | 95 files already formatted |
| P0-8 | mypy clean | PASS | 59 source files, no issues |
| P0-9 | Clean venv reinstall reproducible | PASS | removed .venv, `pip install -e ".[dev]"`, pytest still 457 passed |
| P0-10 | ruff check clean | PASS | `ruff check src tests evals` → All checks passed |

## P1 (open issues, NOT accepted)

| # | Item | Status | Why not accepted |
|---|---|---|---|
| P1-1 | SSE drops events on disconnect | OPEN | Replay from Last-Event-ID implemented; reconnect dedup tested at store level, not over real network drop |
| P1-2 | Restart loses running tasks | OPEN | TaskStore persists to JSON file; running in-memory state still lost on crash |
| P1-3 | NLI classifier is keyword stub | OPEN | CitationVerifier contract tested; no real NLI model accuracy measured |
| P1-4 | No live benchmark numbers | OPEN | No paid calls; no accuracy claim made |
| P1-5 | Rich Markdown rendering | PASS | SafeMarkdown component; HTML injection escaped; [cN] clickable |
| P1-6 | QwenProvider untested against real API | OPEN | Mock transport tested; no real key smoke |
| P1-7 | LangSmith tracing verified end-to-end | PASS | 4 prompts private-pulled by commit hash from LangSmith; minimal trace sent to project "deep research" and confirmed in runs list; `langsmith_repo.py` lookup syntax fixed `@`→`:` per SDK 0.12.6 |
| P1-8 | Feishu export real API | PASS | 使用配置的新目标文件夹完成真实创建；blocks 回查 code=0，2 行非敏感测试正文均存在；未输出或记录凭证 |
| P1-9 | Real browser E2E not run | OPEN | Playwright spec written; chromium binary download slow in this env; not executed here |
| P1-10 | Memory / concurrency scaling not measured | OPEN | See docs/performance-baseline.md |

## P2 (deferred)

- Real token counter (tiktoken) — not integrated; estimate marked `estimated=True`.
- React Query / SWR — plain fetch used.
- Pareto / ablation charts — only bar.html written.
- Docker / production deploy — out of scope.

## Verification commands (real output, clean venv, 2026-09-17)

```
.\.venv\Scripts\python.exe -m pytest
  → 457 passed in 8.57s, 0 warnings
.\.venv\Scripts\ruff.exe check src tests evals
  → All checks passed!
.\.venv\Scripts\ruff.exe format --check src tests evals
  → 95 files already formatted
.\.venv\Scripts\mypy.exe
  → Success: no issues found in 59 source files
cd web; npm test → 3 passed
cd web; npm run build → 31 modules, 149.04 kB
```
