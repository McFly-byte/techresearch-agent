# Demo Guide (5 minutes)

This demo uses the **fake provider** so it works offline, on a laptop, with
no API keys. Do not claim live web results during this demo.

## Setup (do once before the meeting)

```powershell
cd "D:\Programs\Py_proj\DeepResearch Agent"
.\.venv\Scripts\uvicorn.exe api.main:app --port 8000
# new terminal:
cd web; npm run dev
```

Keep both terminals open.

## The 5-minute flow

1. **0:00 – 0:30 Health check**
   Open http://localhost:8000/api/health. Show `status: ok`, `llm.provider: fake`.
   Say: "This proves the backend is up and we're deliberately running on fake
   providers — no paid API calls in this demo."

2. **0:30 – 1:00 Create a task**
   Open the Vite URL (http://localhost:5173). In the Home page, enter:
   > "Compare LangGraph vs LlamaIndex for multi-agent research"
   Depth: `standard`. Click **Start Research**.

3. **1:00 – 2:30 Progress page**
   You should see the event stream: `queued → running → planning → reporting → done`.
   Point out:
   - The SSE envelope (event_id, stage, schema_version).
   - The system does not show chain-of-thought to the user.

4. **2:30 – 4:00 Report page**
   The page auto-redirects to the report. Show:
   - Markdown rendered as `<pre>` (deliberately no rich HTML rendering — XSS-free).
   - Citations with `[1]`, `[2]` markers.
   - The verification status line (verified / contradicted / neutral).

5. **4:00 – 5:00 History + what's real vs fake**
   Show the History page. Then be honest:
   > "The search hits, fetcher, and NLI classifier are deterministic fakes.
   > The orchestration, reducers, CitationVerifier contract, and SSE envelope
   > are real and tested. Live wiring to Tavily and Qwen is in place but not
   > exercised here."

## Fallback if the demo breaks

- Backend not reachable → show `/api/health` curl output, point to
  `tests/unit/test_api_routes.py` as proof the routes work.
- Frontend won't start → show `npm run build` output (production build works).
- Task hangs → show the hard caps in `src/agents/budget.py`; explain every loop
  has a `max_iterations` ceiling.

## What NOT to do

- Don't type "live web search" claims.
- Don't open `.env` on screen.
- Don't show raw LangGraph state (the API never exposes it).
