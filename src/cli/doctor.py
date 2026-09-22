"""`tra` CLI entry point.

Phase 0 subcommands:
- `tra doctor`    鈥?environment/config check. Distinguishes "locally runnable"
                    from "optional external keys missing". Never prints secrets.
- `tra config`    鈥?print Settings.safe_dict() (secrets masked).

Phase 1:
- `tra research "..."` 鈥?run the simple vertical slice. Defaults to fake
                    providers when no Tavily key is set; errors with an
                    actionable message when `--live` is requested without keys.
"""

from __future__ import annotations

import sys
from typing import Any, NoReturn

import click

from core.config import env_file_present, get_settings, has_any_real_key
from core.providers.factory import build_provider
from domain.models import SearchResult


def _check_python() -> tuple[bool, str]:
    info = f"Python {sys.version.split()[0]} on {sys.platform}"
    ok = sys.version_info >= (3, 11)
    return ok, info


@click.group()
def main() -> None:
    """TechResearch Agent CLI."""


@main.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check that the project can run locally; list optional missing keys."""
    py_ok, py_info = _check_python()
    s = get_settings()
    provider = build_provider()

    click.echo("=== TechResearch Agent 鈥?doctor ===")
    click.echo(f"python:    {'OK ' if py_ok else 'OLD'} {py_info}")
    click.echo(f".env:      {'found' if env_file_present() else 'not present (defaults used)'}")
    click.echo(f"provider:  {provider.provider_name} ({provider.model_id})")
    click.echo(f"has keys:  {has_any_real_key()}")

    click.echo("")
    click.echo("Required-for-local-run checks:")
    required_ok = py_ok
    click.echo(f"  [{'OK' if py_ok else 'FAIL'}] Python >= 3.11")
    click.echo(f"  [{'OK'}] provider instantiable ({provider.provider_name})")

    click.echo("")
    click.echo("Optional external integrations (only needed in later phases):")
    optional: list[tuple[str, bool, str]] = [
        ("DashScope / Qwen", s.has_dashscope_key, "Needed in phase 1+ for real LLM calls."),
        ("DeepSeek", s.has_deepseek_key, "Alternative real LLM provider."),
        ("LangSmith trace", s.has_langsmith_key, "Needed for observability (phase 1+)."),
        ("Tavily search", s.has_tavily_search, "Needed for web search (phase 1+)."),
        (
            "Feishu export",
            bool(s.feishu_app_secret.get_secret_value().strip()),
            "Needed for report export (phase 5+).",
        ),
    ]
    for name, present, note in optional:
        marker = "SET " if present else "MISS"
        click.echo(f"  [{marker}] {name:<18} {note}")

    click.echo("")
    if required_ok:
        click.echo("RESULT: local runnable. Missing optional keys above will not block tests/lint.")
        ctx.exit(0)
    else:
        click.echo("RESULT: local NOT runnable 鈥?upgrade Python to 3.11+.")
        ctx.exit(1)


@main.command(name="config")
def show_config() -> NoReturn:
    """Print non-secret settings (all secrets masked)."""
    import json

    click.echo(json.dumps(get_settings().safe_dict(), indent=2, ensure_ascii=False, default=str))
    sys.exit(0)


@main.command()
@click.argument("query")
@click.option("--live", is_flag=True, help="Use live Tavily + arXiv (requires TAVILY_API_KEY).")
@click.option("--papers", is_flag=True, help="Also search arXiv.")
def research(query: str, live: bool, papers: bool) -> None:
    """Run the simple research pipeline on QUERY."""
    import asyncio

    from service.extractor import HeuristicFactExtractor
    from service.report_writer import MarkdownReportWriter
    from service.simple_research import SimpleResearchService
    from tools.fetchers import FakeFetcher, HttpPageFetcher
    from tools.paper_providers import ArxivSearchProvider
    from tools.search_providers import FakeSearchProvider

    s = get_settings()

    web: Any
    fetcher: Any
    paper: Any

    if live:
        if not s.has_tavily_search:
            raise click.UsageError(
                "--live requires TAVILY_API_KEY(S) or TAVILY_PROXY_URL in .env "
                "or drop --live to use fake providers."
            )
        from tools.search_providers import build_search_provider

        web = build_search_provider(s)
        fetcher = HttpPageFetcher()
        paper = ArxivSearchProvider() if papers else None
    else:
        # Fake mode: ship a tiny canned result so the command is demoable offline.
        canned = {
            "langgraph": [
                _fake_hit(
                    "LangGraph overview",
                    "https://example.com/langgraph",
                    "LangGraph is a low-level orchestration framework for stateful agents.",
                ),
                _fake_hit(
                    "LlamaIndex overview",
                    "https://example.com/llamaindex",
                    "LlamaIndex is a data framework for LLM applications with retrieval focus.",
                ),
            ]
        }
        web = FakeSearchProvider(canned)
        fetcher = FakeFetcher(
            mapping={
                "https://example.com/langgraph": (
                    "LangGraph models agents as a graph of nodes. "
                    "It provides typed state, checkpoints and human-in-the-loop. "
                    "It is maintained by LangChain."
                ),
                "https://example.com/llamaindex": (
                    "LlamaIndex focuses on indexing and retrieval over external data. "
                    "It offers high-level agents but less control over state machines. "
                    "It is maintained by LlamaIndex Inc."
                ),
            }
        )
        paper = None

    svc = SimpleResearchService(
        web_search=web,
        paper_search=paper,
        fetcher=fetcher,
        fact_extractor=HeuristicFactExtractor(),
        report_writer=MarkdownReportWriter(),
    )
    result = asyncio.run(svc.run(query, include_papers=papers))
    click.echo(result.report_markdown)
    if result.errors:
        click.echo("")
        click.echo("errors:", err=True)
        for e in result.errors:
            click.echo(f"  - {e}", err=True)


def _fake_hit(title: str, url: str, snippet: str) -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet, source="tavily")


@main.command("run")
@click.argument("query")
@click.option("--context", default="", help="user_context passed to the graph")
@click.option("--depth", default="quick", type=click.Choice(["quick", "standard", "deep"]))
def run_cmd(query: str, context: str, depth: str) -> None:
    """Run the same ResearchRunner as the HTTP API and print the report.

    Shares code with the FastAPI path (src/api/runner.py), so CLI and API
    produce identical results.
    """
    import asyncio

    from api.runner import ResearchRunner
    from api.task_store import TaskStore

    async def _go() -> str:
        store = TaskStore()
        rec = store.create(query=query, user_context=context, depth=depth)
        runner = ResearchRunner(store)
        await runner.run(rec)
        return rec.report_markdown or rec.error

    click.echo(asyncio.run(_go()))


# --- Prompt Hub management --------------------------------------------------


@main.group("prompts")
def prompts_group() -> None:
    """Manage the Prompt Hub (local manifest + LangSmith sync)."""


@prompts_group.command("list")
def prompts_list() -> None:
    """List all registered prompts (name, source, version, variables)."""
    from core.prompts import build_default_registry

    reg = build_default_registry()
    click.echo(f"mode: {reg.mode}")
    for spec in reg.all_specs():
        vars_str = ",".join(spec.variables) or "-"
        click.echo(f"  {spec.name:<30} v{spec.version:<10} vars=[{vars_str}]")


@prompts_group.command("validate")
def prompts_validate() -> None:
    """Validate the local manifest (templates render, variables consistent)."""
    from core.prompts import build_default_registry

    reg = build_default_registry()
    errors = reg.validate()
    if errors:
        click.echo("VALIDATE FAILED:", err=True)
        for e in errors:
            click.echo(f"  - {e}", err=True)
        raise SystemExit(1)
    click.echo(f"validate OK: {len(reg.all_specs())} prompts")


@prompts_group.command("sync")
@click.option("--dry-run", is_flag=True, help="Preview what would be pushed; do not push.")
def prompts_sync(dry_run: bool) -> None:
    """Sync the local manifest to LangSmith (idempotent)."""
    from core.config import get_settings
    from core.prompts import (
        LangSmithPromptRepository,
        PromptRepoError,
        build_default_registry,
        load_lock,
        now_iso,
        save_lock,
    )

    s = get_settings()
    if not s.has_langsmith_key:
        raise click.UsageError("LANGCHAIN_API_KEY is not set. Put it in .env to sync prompts.")
    reg = build_default_registry(force_local=False)
    repo = LangSmithPromptRepository(
        api_key=s.langchain_api_key.get_secret_value(),
        timeout=s.langsmith_prompt_timeout,
    )
    lock = load_lock()
    for spec in reg.all_specs():
        existing = lock.get(spec.name)
        if existing and existing.commit_hash:
            # Already pushed: skip (idempotent). Re-pushing identical content
            # causes a LangSmithConflictError. Use `tra prompts promote` to
            # re-tag, or delete the lock entry to force a fresh push.
            click.echo(f"  {spec.name}: already pushed (commit={existing.commit_hash[:8]}) - skip")
            if dry_run:
                click.echo(f"    [dry-run] would push update for {spec.name}")
            continue
        if dry_run:
            click.echo(f"  {spec.name}: [dry-run] would push (new)")
            continue
        try:
            result = repo.push(spec)
        except PromptRepoError as e:
            click.echo(f"  {spec.name}: PUSH FAILED: {e}", err=True)
            raise SystemExit(1) from e
        lock[spec.name] = type(
            "LE",
            (),
            {
                "name": spec.name,
                "commit_hash": result.commit_hash,
                "tags": result.tags,
                "pushed_at": now_iso(),
                "identifier": result.identifier,
            },
        )()
        click.echo(f"  {spec.name}: pushed commit={result.commit_hash[:8]}")
    save_lock(lock)
    click.echo(f"sync complete ({'dry-run' if dry_run else 'live'})")


@prompts_group.command("pull")
@click.argument("identifier")
def prompts_pull(identifier: str) -> None:
    """Pull a prompt from LangSmith (supports name:tag or name@commit)."""
    from core.config import get_settings
    from core.prompts import LangSmithPromptRepository, PromptRepoError

    s = get_settings()
    if not s.has_langsmith_key:
        raise click.UsageError("LANGCHAIN_API_KEY is not set. Put it in .env to pull prompts.")
    repo = LangSmithPromptRepository(
        api_key=s.langchain_api_key.get_secret_value(),
        timeout=s.langsmith_prompt_timeout,
    )
    try:
        spec = repo.pull(identifier)
    except PromptRepoError as e:
        click.echo(f"pull failed: {e}", err=True)
        raise SystemExit(1) from e
    click.echo(f"name: {spec.name}")
    click.echo(f"version: {spec.version}")
    click.echo(f"variables: {list(spec.variables)}")
    for role, tpl in spec.messages:
        preview = tpl[:80].replace("\n", " ")
        click.echo(f"  [{role}] {preview}...")


@prompts_group.command("promote")
@click.argument("name")
@click.option("--tag", default="production", help="Tag to apply (default: production).")
def prompts_promote(name: str, tag: str) -> None:
    """Mark the current commit of NAME as the given tag (e.g. production)."""
    from core.config import get_settings
    from core.prompts import (
        LangSmithPromptRepository,
        PromptRepoError,
        load_lock,
        now_iso,
        save_lock,
    )

    s = get_settings()
    if not s.has_langsmith_key:
        raise click.UsageError("LANGCHAIN_API_KEY is not set. Put it in .env to promote prompts.")
    lock = load_lock()
    existing = lock.get(name)
    if not existing:
        raise click.UsageError(f"no known prompt {name!r}; run `tra prompts sync` first.")
    repo = LangSmithPromptRepository(
        api_key=s.langchain_api_key.get_secret_value(),
        timeout=s.langsmith_prompt_timeout,
    )
    # Promote = re-push the local manifest with the production commit_tag.
    # This creates a new commit tagged `tag` without changing the template.
    from core.prompts import build_default_registry

    reg = build_default_registry(force_local=True)
    spec = reg.get_local(name)
    try:
        result = repo.push(spec, commit_tags=[tag])
    except PromptRepoError as e:
        click.echo(f"promote failed: {e}", err=True)
        raise SystemExit(1) from e
    # Update lock with the new tag + new commit.
    tags = tuple(sorted(set(existing.tags) | {tag}))
    lock[name] = type(
        "LE",
        (),
        {
            "name": name,
            "commit_hash": result.commit_hash,
            "tags": tags,
            "pushed_at": now_iso(),
            "identifier": result.identifier,
        },
    )()
    save_lock(lock)
    click.echo(f"promoted {name} @ {result.commit_hash[:8]} -> tag={tag}")


if __name__ == "__main__":
    main()
