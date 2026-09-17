# Interview Talking Points

All quantitative claims below are either from the real test suite or marked
"not yet measured". Do not invent percentages.

## 30-second pitch

> "I built a multi-agent research system on LangGraph. A Planner splits a
> technical question into subtasks, a self-rolled Supervisor dispatches Workers
> in parallel up to a concurrency cap, each Worker loops search-fetch-extract
> with observable reflection, and a CitationVerifier **re-fetches every cited
> source** before a 3-way entailment check. 118 offline tests cover the
> reducers, routing, budget, and verifier. Live Tavily/Qwen wiring is in
> place but not run in this repo."

## 3-minute architecture walk

1. **State** (`src/graph/state.py`): TypedDict with explicit reducers for
   `facts`, `citations`, `completed_tasks`. Concurrency rules: list append is
   order-preserving, duplicates by id are dropped, a failed subtask never
   overwrites a completed one.
2. **Supervisor** (`src/agents/supervisor.py`): a **pure function**
   `decide_dispatch(tasks, running, cap)` → list of sends. Routing rules are
   unit-tested without spawning the graph.
3. **Worker loop** (`src/agents/worker.py`): plan query → search → fetch →
   extract → reflect. Reflection uses observable signals (hits, independent
   sources, contradictions, remaining budget), not free-form LLM judgement.
4. **Budget** (`src/agents/budget.py`): warning/critical thresholds. Critical
   stops non-core tasks and writes the info gap into the report.
5. **CitationVerifier** (`src/service/verifier.py`): refetch → context window
   → injectable NLI → verdict. Refetch failure is a separate verdict from
   neutral. Contradictions trigger at most one rewrite round.

## Deep-dive 1: CitationVerifier

- Problem: naive RAG attaches URLs to worker snippets, never re-checks them.
- Design: verifier **re-fetches** the source (does not trust cached snippets),
  extracts a context window around the claim, then runs a 3-way classifier
  (entailment / contradiction / neutral).
- Edge cases tested: refetch failure ≠ neutral; zero-denominator metrics;
  one rewrite round only; unknown citation id rejected.
- Honest limitation: the NLI classifier in this repo is a keyword stub. The
  contract and refetch loop are real; the model is not.

## Deep-dive 2: Supervisor routing

- Why self-rolled? The design asked for a pure, testable dispatcher.
- `decide_dispatch` takes `(pending, running, completed, failed, cap)` and
  returns which tasks to start. Tests prove:
  - 3 independent tasks actually run concurrently (not just labelled parallel).
  - Dependent tasks wait.
  - One failed worker does not cancel siblings.
  - Duplicate `task_id` is a no-op.
- LangGraph 1.2.11 uses `Send` for fan-out; `Command(update=..., goto=[...])`
  does both state update and dispatch in one step.

## Deep-dive 3: Eval harness

- Resumable: per-question JSON, rerun skips done questions (no double-billing).
- Config snapshot: every run records the exact ablation flags.
- Ablations: llm_only / naive_rag / full / no_verifier / no_reflection /
  single_agent. Each flips one knob.
- Honest limitation: only fixture data; no live benchmark numbers claimed.

## Likely follow-up questions

- **"How do you prevent hallucinated citations?"** — `_CitationGuard` rejects
  unknown ids at report build; verifier re-fetches; contradicted claims go to
  a "存疑" section.
- **"What if the LLM says 'enough info'?"** — that is not a stop condition.
  Stop is driven by evidence coverage, contradiction budget, and hard caps.
- **"How do you handle rate limits?"** — classified as retryable with
  Retry-After backoff; auth errors are not retried.
- **"Is this production-ready?"** — no. In-process task store, polling SSE,
  no auth, no Redis/Celery. It is a tested MVP with honest limits.

## Numbers to quote (or not)

- 118 passing offline tests (real).
- 49 source files type-checked by mypy (real).
- Frontend build: 31 modules, ~147 KB JS (real).
- **Do not quote** any Citation Precision / benchmark % — no live eval run.
