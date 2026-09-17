# License Audit

## Project license
- This project: MIT (see LICENSE).
- Does NOT ship as a public release yet; no push has been made.

## Third-party runtime dependencies (representative)
All installed via PyPI. Licenses checked via `pip show`:

| package | license |
|---------|---------|
| fastapi | MIT |
| pydantic / pydantic-settings | MIT |
| langgraph / langchain-core | MIT |
| httpx | BSD |
| trafilatura | Apache-2.0 |
| pypdf | BSD |
| tavily-python | MIT |
| arxiv | MIT |
| uvicorn | BSD |
| starlette | BSD |

No GPL/AGPL dependency detected in the runtime set.

## Datasets
- **DeepResearch Bench II** (https://github.com/imlrz/DeepResearch-Bench-II):
  CC BY 4.0 / CC BY-NC 4.0 (non-commercial).
  - NOT committed to this repo.
  - If used for any public release, must be treated as non-commercial;
    redistribution must preserve attribution and license notice.
- **FixtureDataset** (`evals/adapter.py`): synthetic, original to this repo, MIT.

## .gitignore coverage
- `.venv/`, `node_modules/`, `dist/`, `__pycache__/`, `.env`, `*.db`,
  `evals/results/`, `evals/charts/`, `web/test-results/`,
  `playwright-report/` are covered.
- Large dataset files (if downloaded) should be placed under `evals/data/`
  which is gitignored.

## Secrets / personal info scan
- No `.env` committed. `.gitignore` excludes it.
- No hardcoded API keys in `src/` (verified by `grep -ri "sk-" src/` — none).
- No internal company URLs.
- No personal paths beyond the project root itself.

## OPEN items
- Full transitive license scan of all ~100 wheels not performed;
  `pip-licenses` recommended before public release.
- Playwright browser binaries downloaded to
  `C:\Users\mcfly\AppData\Local\ms-playwright\` are not in the repo.
