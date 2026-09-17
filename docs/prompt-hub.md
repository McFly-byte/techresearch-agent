# Prompt Hub

Unified hosting for all runtime LLM prompts. Replaces hardcoded prompt strings
in Python modules with a versioned, auditable local manifest that can be synced
to LangSmith for team-wide prompt iteration.

## Architecture

```
src/core/prompts/
├── spec.py            # PromptSpec + RenderedPrompt data models
├── manifest.py        # load/save manifest.json + manifest.lock.json
├── langsmith_repo.py  # Thin wrapper over langsmith.Client (push/pull/list)
├── registry.py        # PromptRegistry: local / langsmith / hybrid modes
├── manifest.json      # The auditable local source of truth (committed)
└── manifest.lock.json # Last pushed commit hashes/tags (committed, no secrets)
```

### Modes

| Mode        | Behavior                                              |
|-------------|-------------------------------------------------------|
| `local`     | Force local manifest, zero network. **Default**.       |
| `langsmith` | Force remote; any failure raises.                     |
| `hybrid`    | Try remote, fall back to local on error (source=`local_fallback`). |

Mode is selected by `PROMPT_SOURCE` env var. If `LANGCHAIN_API_KEY` is empty,
the registry always runs in `local` mode regardless of `PROMPT_SOURCE`.

### Runtime prompts (manifest inventory)

| Name                       | Role   | Variables | Purpose                              |
|----------------------------|--------|-----------|--------------------------------------|
| `fact_extraction_system`   | system | none      | LLM fact-extraction system prompt    |
| `fact_extraction_repair`   | user   | none      | One-shot JSON-repair prompt          |

## CLI

```bash
tra prompts list                 # list registered prompts
tra prompts validate             # manifest integrity check (templates render)
tra prompts sync [--dry-run]     # push local manifest to LangSmith (idempotent)
tra prompts pull <name>          # pull a prompt from LangSmith
tra prompts promote <name> --tag production   # tag current commit
```

## Security

- The manifest stores ONLY template text (with `{var}` placeholders). No
  document bodies, no API keys, no user-supplied secrets.
- `push_prompt` sends a `ChatPromptTemplate` object — template text only.
- `RenderedPrompt.variables` records variable NAMES only (never values).
- Tests default to `local` mode (conftest clears `LANGCHAIN_API_KEY`).
- The lock file records commit hashes/tags, never secrets.

## Real smoke (2026-09-17)

- `tra prompts validate` → OK (2 prompts)
- `tra prompts sync --dry-run` → previewed 2 pushes
- `tra prompts sync` → pushed both prompts to private LangSmith repo
  - `fact_extraction_system` commit `17576e9c`
  - `fact_extraction_repair` commit `7e796f5b`
- `tra prompts pull fact_extraction_system` → success, returns ChatPromptTemplate
- `tra prompts promote ... --tag production` → LangSmith returns 409 "nothing to
  commit" on identical content; tag via LangSmith UI or after a template bump.
