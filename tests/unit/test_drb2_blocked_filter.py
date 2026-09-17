"""Regression tests for DRB2 blocked-source + as-of-date filtering.

These tests pin the contract that the research system (a) never fetches/cites
benchmark-forbidden sources (MDPI / IDEAS / ResearchGate / PubMed article
pinned by ``content.blocked``) and (b) drops material dated after the task's
as-of cutoff year.

They are hermetic: the adapter tests write a synthetic JSONL that mimics the
real task7/task51 shape (same blocked URLs, same prompt phrasings) instead of
reading the 2 MB official dataset.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.adapter import (
    DeepResearchBench2Adapter,
    EvalPrompt,
    _parse_as_of_date,
)

from domain.models import Citation, Fact, SearchResult, SourceKind
from service.source_filter import SourcePolicy
from service.verified_report import VerifiedReportBuilder
from service.verifier import CitationVerifier


# --- fixtures ---------------------------------------------------------------
def _cite(cid: str, url: str = "https://clean.example/p", *, title: str = "", snippet: str = "") -> Citation:
    return Citation(
        citation_id=cid,
        kind=SourceKind.WEB,
        locator=url,
        title=title or cid,
        snippet=snippet,
    )


def _fact(fid: str, claim: str, cids: list[str]) -> Fact:
    return Fact(fact_id=fid, claim=claim, source_citation_ids=cids)


class _FakeFetcher:
    """Returns canned text so the heuristic verifier has something to read."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    async def fetch(self, locator: str, *, citation_id: str):  # type: ignore[no-untyped-def]
        from domain.models import SourceDocument

        text = self._m.get(locator, "")
        if not text:
            return SourceDocument(
                citation=_cite(citation_id, locator),
                content="",
                error="not found",
                fetched_ok=False,
            )
        return SourceDocument(
            citation=_cite(citation_id, locator, snippet=text[:200]),
            content=text,
            fetched_ok=True,
        )


# Real blocked URLs / titles copied verbatim from the official DRB2 dataset
# (task7 = land finance, task51 = healthy aging) so the regression stays
# faithful to the benchmark's exact pin.
TASK7_BLOCKED_URLS = [
    "https://www.mdpi.com/2073-445X/11/9/1529",
    "https://ideas.repec.org/a/gam/jlands/v11y2022i9p1529-d911573.html",
    "https://www.researchgate.net/publication/363532173_x",
]
TASK7_BLOCKED_TITLE = (
    "The Local Land Finance Transformation with the Synergy of "
    "Increment and Inventory: A Case Study in China"
)

TASK51_BLOCKED_URLS = [
    "https://www.mdpi.com/1660-4601/19/24/16995",
    "https://pubmed.ncbi.nlm.nih.gov/36554873/",
    "https://www.researchgate.net/publication/366401399_y",
]
TASK51_BLOCKED_TITLE = (
    "Digital-Based Policy and Health Promotion Policy in Japan, the "
    "Republic of Korea, Singapore, and Thailand: A Scoping Review of Policy "
    "Paths to Healthy Aging"
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )


def _row(qid: str, prompt: str, blocked: dict) -> dict:
    return {
        "id": qid,
        "idx": 1,
        "language": "zh",
        "theme": "Finance",
        "prompt": prompt,
        "content": {"task": prompt, "rubric": {}, "blocked": blocked},
        "license": "CC BY 4.0",
    }


TASK7_PROMPT = (
    "请就中国的'土地财政'模式撰写一份研究报告。该报告应涵盖截至2021年的时期。"
    "**important**The following is a rule of highest priority. you are not allowed "
    "to view the following article and urls: {'title': 'TITLE', 'authors': ['A'], "
    "'urls': " + repr(TASK7_BLOCKED_URLS) + "}. Even if you accidentally read it, do not quote it.**"
)

TASK51_PROMPT = (
    "我正在研究亚洲几个国家如何应对数字化和老龄化。请确保你的调研截止到2022年底。"
    "**important**The following is a rule of highest priority. you are not allowed "
    "to view the following article and urls: {'title': 'TITLE', 'authors': ['B'], "
    "'urls': " + repr(TASK51_BLOCKED_URLS) + "}. Even if you accidentally read it, do not quote it.**"
)


# --- 1. Adapter parsing -----------------------------------------------------
def test_adapter_parses_task7_blocked_urls_domains_titles(tmp_path: Path) -> None:
    f = tmp_path / "drb2.jsonl"
    _write_jsonl(
        f,
        [
            _row(
                "task7",
                TASK7_PROMPT,
                {"title": TASK7_BLOCKED_TITLE, "authors": ["Wu"], "urls": TASK7_BLOCKED_URLS},
            )
        ],
    )
    qs = DeepResearchBench2Adapter(f).questions()
    assert len(qs) == 1
    q = qs[0]
    assert q.qid == "task7"
    # Exact blocked URLs preserved.
    for u in TASK7_BLOCKED_URLS:
        assert u in q.blocked_urls
    # Domains normalized: www.mdpi.com -> mdpi.com, www.researchgate.net ->
    # researchgate.net; ideas.repec.org stays as-is.
    assert set(q.blocked_domains) == {"mdpi.com", "ideas.repec.org", "researchgate.net"}
    assert q.blocked_titles == [TASK7_BLOCKED_TITLE]


def test_adapter_parses_task51_blocked_urls_domains_titles(tmp_path: Path) -> None:
    f = tmp_path / "drb2.jsonl"
    _write_jsonl(
        f,
        [
            _row(
                "task51",
                TASK51_PROMPT,
                {"title": TASK51_BLOCKED_TITLE, "authors": ["N"], "urls": TASK51_BLOCKED_URLS},
            )
        ],
    )
    q = DeepResearchBench2Adapter(f).questions()[0]
    assert q.qid == "task51"
    for u in TASK51_BLOCKED_URLS:
        assert u in q.blocked_urls
    assert set(q.blocked_domains) == {
        "mdpi.com",
        "pubmed.ncbi.nlm.nih.gov",
        "researchgate.net",
    }
    assert q.blocked_titles == [TASK51_BLOCKED_TITLE]


def test_adapter_parses_as_of_date_task7_task51(tmp_path: Path) -> None:
    f = tmp_path / "drb2.jsonl"
    _write_jsonl(
        f,
        [
            _row("task7", TASK7_PROMPT, {"urls": []}),
            _row("task51", TASK51_PROMPT, {"urls": []}),
        ],
    )
    qs = {q.qid: q for q in DeepResearchBench2Adapter(f).questions()}
    assert qs["task7"].as_of_date == "2021"
    assert qs["task51"].as_of_date == "2022"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("报告应涵盖截至2021年的时期", "2021"),
        ("请确保你的调研截止到2022年底", "2022"),
        ("截止到2022年底", "2022"),
        ("as of 2021", "2021"),
        ("through 2020", "2020"),
        ("no cutoff here", None),
    ],
)
def test_as_of_date_regex(text: str, expected: str | None) -> None:
    assert _parse_as_of_date(text) == expected


def test_adapter_harvests_urls_from_important_block(tmp_path: Path) -> None:
    """Double-protection: URLs in the prompt's **important** block are merged
    even when content.blocked.urls is empty."""
    prompt = (
        "Research task. you are not allowed to view the following article and "
        "urls: ['https://www.mdpi.com/2073-445X/11/9/1529']."
    )
    f = tmp_path / "drb2.jsonl"
    _write_jsonl(f, [_row("task_x", prompt, {"urls": []})])
    q = DeepResearchBench2Adapter(f).questions()[0]
    assert "https://www.mdpi.com/2073-445X/11/9/1529" in q.blocked_urls
    assert "mdpi.com" in q.blocked_domains


# --- 2. EvalPrompt carries the constraints (answerer isolation contract) ---
def test_eval_prompt_defaults_are_empty() -> None:
    p = EvalPrompt(qid="q", question="what?")
    assert p.blocked_urls == []
    assert p.blocked_domains == []
    assert p.blocked_titles == []
    assert p.as_of_date is None


def test_eval_prompt_accepts_constraints() -> None:
    p = EvalPrompt(
        qid="task7",
        question="土地财政",
        blocked_urls=["https://www.mdpi.com/2073-445X/11/9/1529"],
        blocked_domains=["mdpi.com"],
        blocked_titles=[TASK7_BLOCKED_TITLE],
        as_of_date="2021",
    )
    assert p.as_of_date == "2021"
    assert p.blocked_domains == ["mdpi.com"]


# --- 3. SourcePolicy: URL / domain / title gates ---------------------------
def test_source_policy_blocks_exact_url() -> None:
    pol = SourcePolicy(blocked_urls=["https://www.mdpi.com/2073-445X/11/9/1529"])
    assert pol.is_blocked_url("https://www.mdpi.com/2073-445X/11/9/1529")
    # www. / trailing-slash normalization still hits.
    assert pol.is_blocked_url("https://mdpi.com/2073-445X/11/9/1529/")
    assert not pol.is_blocked_url("https://other.com/foo")


def test_source_policy_blocks_domain_suffix() -> None:
    pol = SourcePolicy(blocked_domains=["mdpi.com", "ideas.repec.org"])
    assert pol.is_blocked_url("https://www.mdpi.com/2073-445X/11/9/1529")
    assert pol.is_blocked_url("https://ideas.repec.org/a/gam/x")
    # A different publisher on the same TLD must NOT be over-blocked.
    assert not pol.is_blocked_url("https://repec.org/other")
    assert not pol.is_blocked_url("https://example.com/mdpi")


def test_source_policy_blocks_title() -> None:
    pol = SourcePolicy(blocked_titles=[TASK7_BLOCKED_TITLE])
    assert pol.is_blocked_title(TASK7_BLOCKED_TITLE)
    # Truncated search title still matches (substring either way).
    assert pol.is_blocked_title("The Local Land Finance Transformation with the Synergy")
    assert not pol.is_blocked_title("Unrelated economics paper")


def test_source_policy_empty_is_noop() -> None:
    pol = SourcePolicy()
    assert pol.is_empty()
    assert not pol.is_blocked_url("https://anything.com/x")
    assert not pol.is_blocked_title("anything")


# --- 4. Search-result filtering -------------------------------------------
def test_filter_search_hits_drops_blocked_domain() -> None:
    pol = SourcePolicy(blocked_domains=["mdpi.com", "researchgate.net"])
    hits = [
        SearchResult(title="Good source", url="https://example.com/a", snippet="ok"),
        SearchResult(title="MDPI article", url="https://www.mdpi.com/2073-445X/11/9/1529", snippet="no"),
        SearchResult(title="RG page", url="https://www.researchgate.net/publication/123", snippet="no"),
    ]
    kept, dropped = pol.filter_search_hits(hits)
    assert dropped == 2
    assert [h.url for h in kept] == ["https://example.com/a"]


def test_filter_search_hits_drops_blocked_title() -> None:
    pol = SourcePolicy(blocked_titles=[TASK7_BLOCKED_TITLE])
    hits = [
        SearchResult(title="Clean", url="https://example.com/a", snippet=""),
        SearchResult(title=TASK7_BLOCKED_TITLE, url="https://example.com/b", snippet=""),
    ]
    kept, dropped = pol.filter_search_hits(hits)
    assert dropped == 1
    assert kept[0].url == "https://example.com/a"


# --- 5. Final citation filtering (VerifiedReportBuilder) -------------------
@pytest.mark.asyncio
async def test_build_drops_blocked_citation_and_fact() -> None:
    blocked = _cite(
        "c_blocked",
        "https://www.mdpi.com/2073-445X/11/9/1529",
        title=TASK7_BLOCKED_TITLE,
    )
    clean = _cite("c_clean", "https://clean.example/land", title="Land finance overview", snippet="data through 2021")
    facts = [
        # This fact cites ONLY the blocked source -> dropped entirely.
        _fact("f_bad", "blocked claim", ["c_blocked"]),
        # This fact cites a clean source -> survives.
        _fact("f_good", "Land finance data through 2021", ["c_clean"]),
    ]
    citations = [blocked, clean]
    fetcher = _FakeFetcher({"https://clean.example/land": "Land finance data through 2021."})
    builder = VerifiedReportBuilder(
        verifier=CitationVerifier(fetcher=fetcher),
        blocked_urls=TASK7_BLOCKED_URLS,
        blocked_domains=["mdpi.com", "researchgate.net", "ideas.repec.org"],
        blocked_titles=[TASK7_BLOCKED_TITLE],
    )
    report = await builder.build(query="土地财政", facts=facts, citations=citations, mode="live")
    # Blocked citation must NOT appear in the report.
    assert "mdpi.com/2073-445X/11/9/1529" not in report.markdown
    assert "researchgate.net" not in report.markdown
    # Clean citation remains.
    assert "clean.example/land" in report.markdown
    # The blocked fact is gone (its claim never rendered).
    assert "blocked claim" not in report.markdown


@pytest.mark.asyncio
async def test_build_drops_post_cutoff_citation() -> None:
    """as_of=2022: a citation whose title/snippet shows 2024 is dropped."""
    old = _cite("c_old", "https://old.example/policy-2021", title="Japan digital policy 2021", snippet="2021 strategy")
    new = _cite("c_new", "https://new.example/policy-2024", title="Japan digital policy 2024", snippet="2024 launch")
    facts = [
        _fact("f1", "Japan had a 2021 strategy", ["c_old"]),
        _fact("f2", "Japan launched in 2024", ["c_new"]),
    ]
    fetcher = _FakeFetcher({"https://old.example/policy-2021": "Japan digital policy 2021."})
    builder = VerifiedReportBuilder(
        verifier=CitationVerifier(fetcher=fetcher),
        as_of_date="2022",
    )
    report = await builder.build(query="数字化", facts=facts, citations=[old, new], mode="live")
    # The 2024 citation is filtered out.
    assert "new.example/policy-2024" not in report.markdown
    # The 2021 citation survives.
    assert "old.example/policy-2021" in report.markdown
    # The post-cutoff audit line is rendered.
    assert "截止日期" in report.markdown


@pytest.mark.asyncio
async def test_build_marks_date_unconfirmed_citation() -> None:
    """A citation with no detectable year is kept but annotated 日期未确认."""
    undated = _cite("c_undated", "https://undated.example/x", title="Policy overview", snippet="some content")
    facts = [_fact("f1", "a policy point", ["c_undated"])]
    fetcher = _FakeFetcher({"https://undated.example/x": "some content about policy."})
    builder = VerifiedReportBuilder(
        verifier=CitationVerifier(fetcher=fetcher),
        as_of_date="2022",
    )
    report = await builder.build(query="数字化", facts=facts, citations=[undated], mode="live")
    # Kept (not dropped).
    assert "undated.example/x" in report.markdown
    # Annotated as date-unconfirmed.
    assert "日期未确认" in report.markdown


@pytest.mark.asyncio
async def test_build_no_constraints_is_noop() -> None:
    """No blocked/as_of args -> citations and facts flow through unchanged."""
    c = _cite("c1", "https://clean.example/p", title="t", snippet="s")
    facts = [_fact("f1", "claim", ["c1"])]
    fetcher = _FakeFetcher({"https://clean.example/p": "claim."})
    builder = VerifiedReportBuilder(verifier=CitationVerifier(fetcher=fetcher))
    report = await builder.build(query="q", facts=facts, citations=[c], mode="live")
    assert "clean.example/p" in report.markdown
    # No audit line when there are no constraints.
    assert "来源约束审计" not in report.markdown


# --- 6. Worker search/fetch gate (via SourcePolicy, integration-lite) ------
def test_worker_policy_filters_blocked_fetch(tmp_path: Path) -> None:
    """The worker consults the same policy: blocked URLs are not fetched."""
    from agents.worker import WorkerNode
    from tools.fetchers import FakeFetcher

    blocked = "https://www.mdpi.com/2073-445X/11/9/1529"
    clean = "https://clean.example/a"

    class _Search:
        def __init__(self):
            self.calls = 0

        async def search(self, query: str, max_results: int = 5):
            return [
                SearchResult(title="blocked", url=blocked, snippet=""),
                SearchResult(title="clean", url=clean, snippet=""),
            ]

    fetcher = FakeFetcher({clean: "clean content about the query"})
    worker = WorkerNode(
        web_search=_Search(),
        fetcher=fetcher,
        blocked_urls=TASK7_BLOCKED_URLS,
        blocked_domains=["mdpi.com", "researchgate.net"],
        blocked_titles=[TASK7_BLOCKED_TITLE],
    )
    # The worker's internal policy must reflect the constraints.
    assert not worker._policy.is_empty()  # type: ignore[attr-defined]
    kept, dropped = worker._policy.filter_search_hits(  # type: ignore[attr-defined]
        [
            SearchResult(title="blocked", url=blocked, snippet=""),
            SearchResult(title="clean", url=clean, snippet=""),
        ]
    )
    assert dropped == 1
    assert kept[0].url == clean


# --- 7. TaskRecord carries the constraints --------------------------------
def test_task_record_defaults_empty() -> None:
    from api.task_store import TaskRecord

    rec = TaskRecord(task_id="t1", query="q")
    assert rec.blocked_urls == []
    assert rec.blocked_domains == []
    assert rec.blocked_titles == []
    assert rec.as_of_date is None


__all__: list[str] = []
