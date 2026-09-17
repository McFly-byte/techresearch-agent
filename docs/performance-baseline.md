# Performance Baseline (FAKE DATA)

> **All numbers below are measured with the offline FakeLLM/FakeSearchProvider.**
> They are NOT production numbers. Real numbers require API keys and are not
> measured in this environment.

## Environment
- Python 3.14.7, Windows 11, local SSD
- No network calls (all fakes)
- `research_depth=quick`

## End-to-end timing (3 runs, fake)

| run | wall time | status | n_events |
|-----|-----------|--------|----------|
| 1   | 0.038s    | completed | 12 |
| 2   | 0.025s    | completed | 12 |
| 3   | 0.025s    | completed | 12 |
| avg | **0.029s** | | |

This is essentially overhead: no LLM, no network, no real fetch. The graph
runs through planner -> worker -> verifier -> writer in ~30ms.

## Stage breakdown (per fake run)

| stage | measured |
|-------|----------|
| planner | <5ms (fake LLM) |
| worker search | <1ms (in-memory dict) |
| worker fetch | <1ms (in-memory dict) |
| extract | <1ms (heuristic) |
| verify | ~10ms (re-fetch from in-memory dict) |
| write | <1ms |

## Call counts (per fake run)
- LLM calls: ~3 (planner + worker reflect) — FakeLLM, zero cost
- Search calls: 1 per subtask
- Fetch calls: 1 per search hit
- Token usage: estimated (chars/4), marked `estimated=True` in BudgetManager

## Concurrency
- `max_workers=2` (default). Fake runs have only 1 subtask at `quick` depth,
  so concurrency was not exercised. Real concurrency scaling is **not
  measured** — marked OPEN.

## Memory
- Not measured. Marked OPEN.

## What this means
- The pipeline is fast offline; the real cost driver is LLM + Tavily calls,
  which this baseline deliberately does not measure.
- No bottleneck to optimize at this scale.
