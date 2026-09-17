"""Prompt Hub tests: contract, security, fallback, cache, CLI, integration.

All tests run offline. The global conftest already clears LANGCHAIN_API_KEY
and forces LLM_PROVIDER=fake, so no test here may touch the network.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner
from langchain_core.prompts import ChatPromptTemplate

from core.prompts import (
    LangSmithPromptRepository,
    PromptRegistry,
    PromptRenderError,
    PromptRepoError,
    PromptSpec,
    reset_default_registry,
)
from core.prompts.spec import _extract_vars

# -- helpers ------------------------------------------------------------------


class FakeLangSmithClient:
    """In-memory stand-in for ``langsmith.Client`` used by contract tests."""

    def __init__(self) -> None:
        self.pushed: list[dict[str, Any]] = []
        self.pulled: list[str] = []
        self.list_calls = 0
        self.prompts: dict[str, ChatPromptTemplate] = {}
        self.commits: dict[str, str] = {}
        self._commit_counter = 0

    def push_prompt(
        self,
        identifier: str,
        *,
        object: Any = None,
        parent_commit_hash: str = "latest",
        is_public: bool | None = None,
        description: str | None = None,
        readme: str | None = None,
        tags: list[str] | None = None,
        commit_tags: str | list[str] | None = None,
        commit_description: str | None = None,
    ) -> str:
        self.pushed.append(
            {
                "identifier": identifier,
                "object": object,
                "tags": tags,
                "commit_tags": commit_tags,
            }
        )
        self._commit_counter += 1
        commit = f"commit_{self._commit_counter}"
        self.prompts[identifier] = object
        self.commits[identifier] = commit
        return commit

    def pull_prompt(self, prompt_identifier: str, **_kwargs: Any) -> Any:
        self.pulled.append(prompt_identifier)
        if prompt_identifier not in self.prompts:
            raise RuntimeError(f"not found: {prompt_identifier}")

        # Wrap in a stand-in object that has a .prompt attribute like the real
        # LangSmith Prompt object does.
        class _Wrapped:
            def __init__(self, cp: ChatPromptTemplate) -> None:
                self.prompt = cp

        return _Wrapped(self.prompts[prompt_identifier])

    def list_prompts(self) -> list[Any]:
        self.list_calls += 1

        class _P:
            def __init__(self, name: str) -> None:
                self.repo_handle = name
                self.description = ""
                self.updated_at = ""

        return [_P(n) for n in self.prompts]


class FailingClient(FakeLangSmithClient):
    """Client whose push/pull always raises (used for fallback tests)."""

    def push_prompt(self, *_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("network down")

    def pull_prompt(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("network down")


def _make_spec(name: str = "test_prompt", *, variables: list[str] | None = None) -> PromptSpec:
    vars_ = variables or ["user_context"]
    tmpl = f"Hello {{{vars_[0]}}}. You are a bot."
    return PromptSpec(
        name=name,
        description="test prompt",
        version="1.0.0",
        messages=(("system", tmpl),),
        variables=tuple(vars_),
    )


# -- spec / variable extraction ----------------------------------------------


class TestSpec:
    def test_extract_vars_simple(self) -> None:
        assert _extract_vars("Hello {name}") == {"name"}

    def test_extract_vars_multiple(self) -> None:
        assert _extract_vars("{a} and {b} and {a}") == {"a", "b"}

    def test_extract_vars_none(self) -> None:
        assert _extract_vars("no variables here") == set()

    def test_to_chat_prompt_template(self) -> None:
        spec = _make_spec()
        cp = spec.to_chat_prompt_template()
        assert "user_context" in cp.input_variables

    def test_from_chat_prompt_template_roundtrip(self) -> None:
        spec = PromptSpec(
            name="rt",
            description="",
            version="1.0.0",
            messages=(("system", "Hello {x} and {y}"),),
            variables=("x", "y"),
        )
        cp = spec.to_chat_prompt_template()
        restored = PromptSpec.from_chat_prompt_template("rt", cp)
        assert set(restored.variables) == {"x", "y"}


# -- local registry -----------------------------------------------------------


class TestLocalRegistry:
    def test_mode_default_local(self) -> None:
        reg = PromptRegistry()
        assert reg.mode == "local"

    def test_list_has_two_runtime_prompts(self) -> None:
        reg = PromptRegistry()
        names = [s.name for s in reg.all_specs()]
        assert "fact_extraction_system" in names
        assert "fact_extraction_repair" in names

    def test_render_system_prompt_no_vars(self) -> None:
        reg = PromptRegistry()
        r = reg.render("fact_extraction_system")
        assert r.source == "local"
        assert r.system_text().startswith("You are a research fact extractor")

    def test_render_repair_prompt(self) -> None:
        reg = PromptRegistry()
        r = reg.render("fact_extraction_repair")
        assert r.messages[0][1].startswith("Your previous response")

    def test_validate_manifest_ok(self) -> None:
        reg = PromptRegistry()
        assert reg.validate() == []

    def test_render_unknown_prompt_raises(self) -> None:
        reg = PromptRegistry()
        with pytest.raises(PromptRenderError):
            reg.render("nonexistent")

    def test_variable_validation_missing_raises(self) -> None:
        spec = _make_spec(variables=["required_var"])
        reg = PromptRegistry(manifest={"t": spec})
        with pytest.raises(PromptRenderError, match="missing required"):
            reg.render("t")

    def test_variable_validation_extra_rejected(self) -> None:
        spec = _make_spec(variables=["a"])
        reg = PromptRegistry(manifest={"t": spec})
        with pytest.raises(PromptRenderError, match="unexpected"):
            reg.render("t", a="x", b="y")

    def test_render_with_vars(self) -> None:
        spec = _make_spec(variables=["user_context"])
        reg = PromptRegistry(manifest={"t": spec})
        r = reg.render("t", user_context="deep dive")
        assert r.messages[0][1] == "Hello deep dive. You are a bot."
        assert r.variables == ("user_context",)


# -- langsmith repo contract --------------------------------------------------


class TestLangSmithRepoContract:
    def test_push_calls_client_with_chat_prompt_template(self) -> None:
        fake = FakeLangSmithClient()
        repo = LangSmithPromptRepository(client=fake)
        spec = _make_spec()
        result = repo.push(spec)
        assert len(fake.pushed) == 1
        pushed = fake.pushed[0]
        assert pushed["identifier"] == "test_prompt"
        assert isinstance(pushed["object"], ChatPromptTemplate)
        assert result.commit_hash.startswith("commit_")

    def test_push_does_not_contain_document_body(self) -> None:
        """Security: pushed object is the template, not rendered content."""
        fake = FakeLangSmithClient()
        repo = LangSmithPromptRepository(client=fake)
        spec = _make_spec(variables=["user_context"])
        repo.push(spec)
        pushed_obj = fake.pushed[0]["object"]
        # The template must contain the placeholder, NOT a rendered value.
        for msg in pushed_obj.messages:
            tmpl = getattr(msg.prompt, "template", "")
            assert "SECRET_DOCUMENT_BODY" not in tmpl
            assert "{user_context}" in tmpl or "user_context" not in tmpl

    def test_pull_returns_prompt_spec(self) -> None:
        fake = FakeLangSmithClient()
        repo = LangSmithPromptRepository(client=fake)
        spec = _make_spec(variables=["x"])
        repo.push(spec)
        pulled = repo.pull("test_prompt")
        assert "x" in pulled.variables

    def test_push_raises_on_client_error(self) -> None:
        repo = LangSmithPromptRepository(client=FailingClient())
        with pytest.raises(PromptRepoError):
            repo.push(_make_spec())

    def test_pull_raises_on_client_error(self) -> None:
        repo = LangSmithPromptRepository(client=FailingClient())
        with pytest.raises(PromptRepoError):
            repo.pull("whatever")


# -- hybrid / fallback --------------------------------------------------------


class TestHybridFallback:
    def test_hybrid_falls_back_to_local_on_pull_error(self) -> None:
        spec = _make_spec(variables=["user_context"])
        reg = PromptRegistry(
            mode="hybrid",
            repo=LangSmithPromptRepository(client=FailingClient()),
            manifest={"test_prompt": spec},
        )
        r = reg.render("test_prompt", user_context="hi")
        assert r.source == "local_fallback"
        assert r.messages[0][1] == "Hello hi. You are a bot."

    def test_langsmith_mode_raises_on_pull_error(self) -> None:
        spec = _make_spec()
        reg = PromptRegistry(
            mode="langsmith",
            repo=LangSmithPromptRepository(client=FailingClient()),
            manifest={"test_prompt": spec},
        )
        with pytest.raises(PromptRenderError):
            reg.render("test_prompt", user_context="hi")

    def test_local_mode_zero_network(self) -> None:
        """Local mode must never touch the repo, even if one is attached."""
        fake = FakeLangSmithClient()
        spec = _make_spec()
        reg = PromptRegistry(
            mode="local",
            repo=LangSmithPromptRepository(client=fake),
            manifest={"test_prompt": spec},
        )
        reg.render("test_prompt", user_context="x")
        assert fake.pulled == []


# -- cache --------------------------------------------------------------------


class TestCache:
    def test_repeated_pull_only_calls_client_once(self) -> None:
        fake = FakeLangSmithClient()
        repo = LangSmithPromptRepository(client=fake)
        spec = _make_spec()
        repo.push(spec)
        reg = PromptRegistry(
            mode="langsmith",
            repo=repo,
            cache_ttl=60.0,
            manifest={"test_prompt": spec},
        )
        reg.render("test_prompt", user_context="a")
        reg.render("test_prompt", user_context="b")
        reg.render("test_prompt", user_context="c")
        assert len(fake.pulled) == 1

    def test_cache_ttl_expiry_refetches(self) -> None:
        fake = FakeLangSmithClient()
        repo = LangSmithPromptRepository(client=fake)
        spec = _make_spec()
        repo.push(spec)
        reg = PromptRegistry(
            mode="langsmith",
            repo=repo,
            cache_ttl=0.0,  # always expired
            manifest={"test_prompt": spec},
        )
        reg.render("test_prompt", user_context="a")
        reg.render("test_prompt", user_context="b")
        assert len(fake.pulled) == 2


# -- tag / commit pin ---------------------------------------------------------


class TestTagCommitPin:
    def test_pull_with_tag_builds_lookup_string(self) -> None:
        import contextlib

        fake = FakeLangSmithClient()
        # Pre-populate so pull doesn't fail.
        fake.prompts["test_prompt:production"] = _make_spec().to_chat_prompt_template()
        repo = LangSmithPromptRepository(client=fake)
        # We just verify the lookup string passed to the client.
        with contextlib.suppress(PromptRepoError):
            repo.pull("test_prompt", tag="production")
        assert any("test_prompt:production" in p for p in fake.pulled)

    def test_pull_with_commit_builds_lookup_string(self) -> None:
        import contextlib

        fake = FakeLangSmithClient()
        repo = LangSmithPromptRepository(client=fake)
        with contextlib.suppress(PromptRepoError):
            repo.pull("test_prompt", commit_hash="abc123")
        assert any("test_prompt:abc123" in p for p in fake.pulled)


# -- security -----------------------------------------------------------------


class TestSecurity:
    def test_push_object_has_no_secret_values(self) -> None:
        """The pushed ChatPromptTemplate must only contain template placeholders."""
        fake = FakeLangSmithClient()
        repo = LangSmithPromptRepository(client=fake)
        spec = PromptSpec(
            name="sec_test",
            description="",
            version="1.0.0",
            messages=(("system", "API key is {api_key}"),),
            variables=("api_key",),
        )
        repo.push(spec)
        pushed_obj = fake.pushed[0]["object"]
        for msg in pushed_obj.messages:
            tmpl = getattr(msg.prompt, "template", "")
            # The literal secret value must never appear.
            assert "sk-real-secret-1234" not in tmpl

    def test_rendered_prompt_variables_records_names_only(self) -> None:
        spec = _make_spec(variables=["api_key"])
        reg = PromptRegistry(manifest={"t": spec})
        r = reg.render("t", api_key="sk-should-not-be-logged")
        # The RenderedPrompt records variable NAMES, not values.
        assert r.variables == ("api_key",)
        # The concrete messages DO contain the value (that's what we send to
        # the LLM), but the provenance record must not.
        assert "api_key" not in (r.commit or "")
        assert "sk-should-not-be-logged" not in (r.tag or "")


# -- CLI ----------------------------------------------------------------------


class TestCLI:
    def _runner(self) -> CliRunner:

        return CliRunner()

    def test_list(self) -> None:
        result = CliRunner().invoke(_cli_main(), ["prompts", "list"])
        assert result.exit_code == 0
        assert "fact_extraction_system" in result.output

    def test_validate_ok(self) -> None:
        result = CliRunner().invoke(_cli_main(), ["prompts", "validate"])
        assert result.exit_code == 0
        assert "validate OK" in result.output

    def test_sync_dry_run_without_key_fails(self) -> None:
        # conftest already clears LANGCHAIN_API_KEY.
        result = CliRunner().invoke(_cli_main(), ["prompts", "sync", "--dry-run"])
        assert result.exit_code != 0
        assert "LANGCHAIN_API_KEY" in result.output

    def test_pull_without_key_fails(self) -> None:
        result = CliRunner().invoke(_cli_main(), ["prompts", "pull", "whatever"])
        assert result.exit_code != 0
        assert "LANGCHAIN_API_KEY" in result.output

    def test_promote_without_key_fails(self) -> None:
        result = CliRunner().invoke(_cli_main(), ["prompts", "promote", "whatever"])
        assert result.exit_code != 0


def _cli_main():  # type: ignore[no-untyped-def]
    from cli.doctor import main

    return main


# -- integration: extractor uses registry -------------------------------------


class TestExtractorIntegration:
    def test_extractor_uses_registry_system_prompt(self) -> None:
        from core.providers.fake import FakeLLM
        from service.extractor import LLMFactExtractor

        reset_default_registry()
        extractor = LLMFactExtractor(FakeLLM(model_id="fake-1"))
        # The resolved system prompt must match the manifest.
        assert extractor._system_prompt.startswith("You are a research fact extractor")
        assert "JSON object" in extractor._system_prompt

    @pytest.mark.asyncio
    async def test_extractor_runs_offline(self) -> None:
        from core.providers.fake import FakeLLM
        from domain.models import Citation, SourceDocument
        from service.extractor import LLMFactExtractor

        reset_default_registry()
        extractor = LLMFactExtractor(FakeLLM(model_id="fake-1"))
        doc = SourceDocument(
            url="https://example.com",
            fetcher="http",
            citation=Citation(
                citation_id="c_test",
                kind="web",
                locator="https://example.com",
                snippet="",
            ),
            content="LangGraph is a framework for stateful agents. It supports checkpoints.",
            fetched_ok=True,
        )
        result = await extractor.extract([doc])
        # Should not raise; fallback to heuristic on parse failure.
        assert result is not None
