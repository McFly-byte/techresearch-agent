"""Verified report builder: facts -> claims -> verify -> one revision -> MD/HTML.

Stage 4 P0-5 rewrite:
  - RevisionPolicy (deterministic, injectable): a contradicted claim is
    DROPPED (not rendered as a fact); a neutral claim is downgraded to
    "uncertain" phrasing that does NOT re-assert the original fact. The
    revision does NOT stitch a prefix onto the claim and then re-verify the
    stitched sentence (which can never entail).
  - Every revision records a RevisionRecord(old_text, new_text, reason,
    citation_ids) so the audit trail is explicit.
  - The re-verify pass runs EXACTLY ONCE per claim (2 verify calls per
    non-verified claim total).
  - verification_log entries carry ``round="initial"`` / ``round="revision"``.
  - refetch_failed claims land in the limitations section and are NOT
    re-asserted as facts.
  - HTML rendering emits safe http/https links only; dangerous schemes
    (javascript:/data:) are rendered as inert plain text.
"""

from __future__ import annotations

import difflib
import html as _html
import logging
import re
from dataclasses import dataclass, field

from core.exceptions import ToolError
from core.prompts import PromptRegistry, get_default_registry, load_lock
from core.providers.base import BaseLLMProvider, LLMResponse, Message
from core.tracing import TracingContext, noop_tracing
from domain.models import Citation, Fact
from domain.verification import (
    Claim,
    VerificationMetrics,
    VerificationResult,
)
from service.source_filter import SourcePolicy
from service.verifier import CitationVerifier

log = logging.getLogger(__name__)

_SAFE_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

# --- Report quality gate -----------------------------------------------------
# Bracketed numeric citation tags as demanded by the synthesis prompt: [c1].
_CITE_TAG_RE = re.compile(r"\[c(\d+)\]")
_MIN_REPORT_LEN = 100
_ECHO_WINDOW = 200
_ECHO_RATIO = 0.8


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _looks_like_prompt_echo(report_md: str, query: str) -> bool:
    """True when the report just restates the user question.

    Compares the normalized first _ECHO_WINDOW chars of both strings with a
    SequenceMatcher.ratio > _ECHO_RATIO threshold (whitespace-insensitive).
    """
    a = _normalize(report_md)[:_ECHO_WINDOW]
    b = _normalize(query)[:_ECHO_WINDOW]
    if not a or not b:
        return False
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio > _ECHO_RATIO


def check_report_quality(report_md: str, query: str, citations: list[Citation]) -> dict:
    """Quality gate for an LLM-synthesized research report.

    Returns a dict with:
      passed, non_empty, not_prompt_echo, has_citation_tags,
      citations_traceable, citations_nonempty, failure_reason.
    """
    non_empty = len((report_md or "").strip()) > _MIN_REPORT_LEN
    not_prompt_echo = not _looks_like_prompt_echo(report_md or "", query or "")
    tags = _CITE_TAG_RE.findall(report_md or "")
    has_citation_tags = bool(tags)
    known_ids = {c.citation_id for c in citations} if citations else set()
    citations_traceable = bool(tags) and all(f"c{n}" in known_ids for n in tags)
    citations_nonempty = bool(citations)

    failed: list[str] = []
    if not non_empty:
        failed.append("non_empty")
    if not not_prompt_echo:
        failed.append("not_prompt_echo")
    if not has_citation_tags:
        failed.append("has_citation_tags")
    if not citations_traceable:
        failed.append("citations_traceable")
    if not citations_nonempty:
        failed.append("citations_nonempty")

    return {
        "passed": not failed,
        "non_empty": non_empty,
        "not_prompt_echo": not_prompt_echo,
        "has_citation_tags": has_citation_tags,
        "citations_traceable": citations_traceable,
        "citations_nonempty": citations_nonempty,
        "failure_reason": "" if not failed else f"quality gate failed: {', '.join(failed)}",
    }


@dataclass
class RevisionRecord:
    """One controlled revision applied to a claim after the initial verify."""

    claim_id: str
    old_text: str
    new_text: str
    reason: str
    citation_ids: list[str]


@dataclass
class VerifiedReport:
    markdown: str
    html: str
    claims: list[Claim]
    metrics: VerificationMetrics
    verification_log: list[VerificationResult] = field(default_factory=list)
    revision_log: list[RevisionRecord] = field(default_factory=list)
    # The citations that SURVIVED the source policy (blocked / post-cutoff
    # already pruned). Exposed to the runner/eval so the harness can record
    # the real provenance the report relied on.
    citations: list[Citation] = field(default_factory=list)
    # Quality-gate result for the LLM-synthesized report. EMPTY when the
    # builder fell back to the deterministic template (llm_provider=None,
    # e.g. fake mode). When populated, "passed" decides whether the runner
    # marks the task failed.
    synthesis_quality: dict = field(default_factory=dict)


def facts_to_claims(facts: list[Fact]) -> list[Claim]:
    """One claim per fact. Citation ids come straight from the fact."""
    out: list[Claim] = []
    for i, f in enumerate(facts, start=1):
        out.append(
            Claim(
                claim_id=f"c{i}",
                claim_text=f.claim,
                section_id="body",
                citation_ids=list(f.source_citation_ids),
                claim_type="factual",
            )
        )
    return out


class RevisionPolicy:
    """Deterministic revision policy.

    Rules:
    - contradicted  -> DROP the claim (do not render it as a fact).
    - neutral       -> downgrade to uncertainty phrasing that does NOT re-assert.
    - refetch_failed -> leave as-is; the builder lists it in limitations.
    """

    def revise(
        self, claim: Claim, result: VerificationResult
    ) -> tuple[Claim, RevisionRecord | None]:
        if result.verdict == "contradiction":
            new = claim.model_copy(
                update={
                    "verification_status": "dropped",
                    "verification_note": result.reason,
                }
            )
            return new, RevisionRecord(
                claim_id=claim.claim_id,
                old_text=claim.claim_text,
                new_text="",  # dropped
                reason="contradicted: source contradicts claim; claim dropped",
                citation_ids=list(claim.citation_ids),
            )
        if result.verdict == "neutral":
            uncertain = f"有待确认：{claim.claim_text}（证据不足，未获来源支持）"
            new = claim.model_copy(
                update={
                    "claim_text": uncertain,
                    "verification_status": "neutral",
                    "verification_note": result.reason,
                }
            )
            return new, RevisionRecord(
                claim_id=claim.claim_id,
                old_text=claim.claim_text,
                new_text=uncertain,
                reason="neutral: insufficient evidence; downgraded to uncertainty",
                citation_ids=list(claim.citation_ids),
            )
        # refetch_failed or other: leave the original text, mark status.
        new = claim.model_copy(
            update={"verification_status": "refetch_failed", "verification_note": result.reason}
        )
        return new, None


class VerifiedReportBuilder:
    def __init__(
        self,
        verifier: CitationVerifier | None = None,
        *,
        revision_policy: RevisionPolicy | None = None,
        blocked_urls: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        blocked_titles: list[str] | None = None,
        as_of_date: str | None = None,
        llm_provider: BaseLLMProvider | None = None,
        tracing: TracingContext | None = None,
        registry: PromptRegistry | None = None,
    ) -> None:
        self._verifier = verifier or CitationVerifier()
        self._policy = revision_policy or RevisionPolicy()
        # DRB2 source constraints: blocked sources are dropped from the final
        # citation list AND every fact that relied on them is pruned. Material
        # dated after ``as_of_date`` is dropped as post_cutoff.
        self._source_policy = SourcePolicy(
            blocked_urls=list(blocked_urls or []),
            blocked_domains=list(blocked_domains or []),
            blocked_titles=list(blocked_titles or []),
            as_of_date=as_of_date,
        )
        # LLM report synthesis (optional). None -> deterministic template
        # rendering (fake mode / offline tests stay offline and hermetic).
        self._llm_provider = llm_provider
        self._tracing = tracing or noop_tracing()
        self._registry = registry or get_default_registry()
        self._system_prompt = self._registry.render("report_synthesis_system").system_text()
        # LangSmith prompt association: the single synthesis call is wrapped in
        # an llm_prompt_span per prompt name; commits come from the lock file.
        self._prompt_commits: dict[str, str] = {}
        try:
            for name, entry in load_lock().items():
                self._prompt_commits[name] = entry.commit_hash
        except Exception:  # noqa: BLE001
            self._prompt_commits = {}
        # Per-call usage of the LAST synthesis LLM call (None in template mode).
        self.last_synthesis_usage: LLMResponse | None = None

    def _apply_source_policy(
        self, facts: list[Fact], citations: list[Citation]
    ) -> tuple[list[Fact], list[Citation], set[str], int, int]:
        """Drop blocked / post-cutoff citations and the facts that depend on them.

        Returns ``(kept_facts, kept_citations, date_unconfirmed_ids, n_blocked,
        n_post_cutoff)``. Facts whose surviving citation set becomes empty are
        dropped; facts that also cite a clean source keep only the clean ids.
        """
        if self._source_policy.is_empty():
            date_unconfirmed: set[str] = set()
            return list(facts), list(citations), date_unconfirmed, 0, 0

        kept_citations: list[Citation] = []
        removed_ids: set[str] = set()
        date_unconfirmed_ids: set[str] = set()
        n_blocked = 0
        n_post_cutoff = 0
        for c in citations:
            keep, reason = self._source_policy.citation_decision(c)
            if keep:
                kept_citations.append(c)
                if self._source_policy.is_date_unconfirmed(c):
                    date_unconfirmed_ids.add(c.citation_id)
                continue
            removed_ids.add(c.citation_id)
            if reason == "post_cutoff":
                n_post_cutoff += 1
            else:
                n_blocked += 1

        kept_facts: list[Fact] = []
        for f in facts:
            surviving = [c for c in f.source_citation_ids if c not in removed_ids]
            if not surviving:
                continue  # every supporting citation was blocked/post-cutoff
            if len(surviving) == len(f.source_citation_ids):
                kept_facts.append(f)
            else:
                kept_facts.append(f.model_copy(update={"source_citation_ids": surviving}))
        return kept_facts, kept_citations, date_unconfirmed_ids, n_blocked, n_post_cutoff

    # -- LLM report synthesis ------------------------------------------------
    async def _synthesize_report(
        self,
        *,
        query: str,
        revised_claims: list[Claim],
        citations: list[Citation],
        date_unconfirmed_ids: set[str],
        n_blocked: int,
        n_post_cutoff: int,
    ) -> tuple[str, dict]:
        """Synthesize a structured research report with the LLM.

        The input citations are RENUMBERED to clean ``c1..cN`` ids (real graph
        ids look like ``c_worker1_2`` and are un-citeable in [cN] form); the
        verified facts are mapped onto those new ids. The quality gate runs on
        the raw LLM output and is returned alongside it.
        """
        assert self._llm_provider is not None  # noqa: S101
        # --- Input size guard ---------------------------------------------------
        # Synthesis sends query + facts + citations to the LLM.  A long DRB2
        # question (≈2k chars) plus 20+ facts and 20+ citations can exceed
        # 15k chars, which causes Qwen to time out even at 300s.  Truncate to
        # a safe budget: top-15 facts, only citations referenced by those facts,
        # each field length-capped.
        _MAX_FACTS = 15
        _MAX_CITATIONS = 15
        _FACT_CHARS = 300
        _CITE_TITLE_CHARS = 200

        # Sort: verified first, then neutral, then refetch_failed.
        _status_order = {"verified": 0, "neutral": 1, "refetch_failed": 2}
        sorted_claims = sorted(
            [c for c in revised_claims if c.verification_status != "dropped" and c.claim_text.strip()],
            key=lambda c: _status_order.get(c.verification_status, 9),
        )[:_MAX_FACTS]

        # Only keep citations referenced by the kept facts.
        kept_cit_ids: set[str] = set()
        for c in sorted_claims:
            kept_cit_ids.update(c.citation_ids)
        filtered_citations = [cit for cit in citations if cit.citation_id in kept_cit_ids][:_MAX_CITATIONS]
        filtered_cit_ids = {cit.citation_id for cit in filtered_citations}

        old_to_simple: dict[str, str] = {}
        simple_citations: list[Citation] = []
        for i, cit in enumerate(filtered_citations, start=1):
            sid = f"c{i}"
            old_to_simple[cit.citation_id] = sid
            title = (cit.title or cit.locator or "")[:_CITE_TITLE_CHARS]
            simple_citations.append(cit.model_copy(update={"citation_id": sid, "title": title}))

        facts_lines: list[str] = []
        for seq, c in enumerate(sorted_claims, start=1):
            sids = [old_to_simple[x] for x in c.citation_ids if x in old_to_simple and x in filtered_cit_ids]
            status_label = {
                "verified": "verified",
                "neutral": "uncertain",
                "refetch_failed": "evidence_unavailable",
            }.get(c.verification_status, c.verification_status)
            claim_text = c.claim_text[:_FACT_CHARS]
            facts_lines.append(
                f"{seq}. {claim_text} [{', '.join(sids)}]  (status: {status_label})"
            )
        verified_facts_block = "\n".join(facts_lines) if facts_lines else "(no verified facts)"
        citations_block = "\n".join(
            f"[{cit.citation_id}] {cit.title or cit.locator} — {cit.locator}"
            for cit in simple_citations
        )

        cons: list[str] = []
        if self._source_policy.as_of_date:
            cons.append(
                f"- as-of date: {self._source_policy.as_of_date}. "
                "Do NOT use information published after this date."
            )
        if n_blocked:
            cons.append(
                f"- {n_blocked} blocked source(s) were removed before synthesis; "
                "do not cite or mention them."
            )
        if n_post_cutoff:
            cons.append(
                f"- {n_post_cutoff} post-cutoff source(s) were removed; do not cite them."
            )
        if date_unconfirmed_ids:
            cons.append("- Some sources have unconfirmed publication dates; phrase claims carefully.")
        cons.append("- Write the report in the SAME language as the user question.")
        constraints_block = "\n".join(cons)

        rendered_user = self._registry.render(
            "report_synthesis_user",
            query=query,
            verified_facts=verified_facts_block,
            citations=citations_block,
            constraints=constraints_block,
        )
        messages = [Message(role="system", content=self._system_prompt)]
        for _role, content in rendered_user.messages:
            messages.append(Message(role="user", content=content))

        total_chars = sum(len(m.content) for m in messages)
        log.info(
            "report_synthesis_input chars=%d facts=%d citations=%d",
            total_chars, len(revised_claims), len(citations),
        )

        sys_commit = self._prompt_commits.get("report_synthesis_system", "")
        user_commit = self._prompt_commits.get("report_synthesis_user", "")
        with self._tracing.llm_prompt_span(
            "report_synthesis_system", sys_commit
        ), self._tracing.llm_prompt_span("report_synthesis_user", user_commit):
            resp = await self._llm_provider.acomplete(messages, max_tokens=2048)
        self.last_synthesis_usage = resp
        md = (resp.text or "").strip()
        quality = check_report_quality(md, query=query, citations=simple_citations)
        return md, quality

    async def build(
        self,
        *,
        query: str,
        facts: list[Fact],
        citations: list[Citation],
        mode: str = "live",
    ) -> VerifiedReport:
        # Source gate (DRB2): drop blocked-source citations and post-cutoff
        # material BEFORE verification so the verifier never even sees a
        # forbidden source. Facts that lose all their supporting citations are
        # dropped; facts with mixed citations keep only the clean ids.
        facts, citations, date_unconfirmed_ids, n_blocked, n_post_cutoff = (
            self._apply_source_policy(facts, citations)
        )

        # Guard: no unknown citation ids.
        known = {c.citation_id for c in citations}
        for f in facts:
            missing = [c for c in f.source_citation_ids if c not in known]
            if missing:
                raise ToolError(f"unknown citation ids {missing}")

        claims = facts_to_claims(facts)
        results: list[VerificationResult] = []
        revision_log: list[RevisionRecord] = []
        revised: list[Claim] = []
        # Track which claims need a re-verify (contradiction/neutral).
        to_reverify: list[int] = []

        for idx, claim in enumerate(claims):
            r = await self._verifier.verify(claim, citations, round_label="initial")
            results.append(r)
            if r.verdict == "entailment":
                revised.append(
                    claim.model_copy(
                        update={
                            "verification_status": "verified",
                            "verification_note": r.reason,
                        }
                    )
                )
            elif r.verdict is None:
                # refetch failed: mark, do not re-assert.
                revised.append(
                    claim.model_copy(
                        update={
                            "verification_status": "refetch_failed",
                            "verification_note": r.reason,
                        }
                    )
                )
            else:
                # contradicted or neutral -> apply revision policy.
                new_claim, rec = self._policy.revise(claim, r)
                revised.append(new_claim)
                if rec is not None:
                    revision_log.append(rec)
                    if new_claim.verification_status != "dropped":
                        to_reverify.append(idx)

        # ONE re-verify round on the claims we revised (exactly 1 loop).
        for idx in to_reverify:
            re_result = await self._verifier.verify(revised[idx], citations, round_label="revision")
            results.append(re_result)
            current = revised[idx]
            if re_result.verdict == "entailment":
                revised[idx] = current.model_copy(
                    update={
                        "verification_status": "verified",
                        "verification_note": f"revised+reverified: {re_result.reason}",
                    }
                )
            else:
                # Unresolved after revision: keep the downgraded status.
                revised[idx] = current.model_copy(
                    update={"verification_note": f"unresolved: {re_result.reason}"}
                )

        metrics = _compute_metrics(revised)
        html_str = render_html(
            query=query,
            claims=revised,
            citations=citations,
            metrics=metrics,
            mode=mode,
            date_unconfirmed_ids=date_unconfirmed_ids,
        )
        # Report rendering: LLM synthesis when a provider was injected (live
        # mode), otherwise the deterministic template (fake / offline tests).
        synth_quality: dict = {}
        if self._llm_provider is not None:
            try:
                md, synth_quality = await self._synthesize_report(
                    query=query,
                    revised_claims=revised,
                    citations=citations,
                    date_unconfirmed_ids=date_unconfirmed_ids,
                    n_blocked=n_blocked,
                    n_post_cutoff=n_post_cutoff,
                )
            except Exception as e:  # noqa: BLE001
                # A synthesis outage must not crash the run: fall back to the
                # deterministic template and record the failure so the quality
                # gate (and therefore the runner) flags the task.
                log.warning(
                    "report_synthesis_failed error_type=%s error_msg=%s",
                    type(e).__name__, str(e)[:500],
                )
                md = render_markdown(
                    query=query,
                    claims=revised,
                    citations=citations,
                    metrics=metrics,
                    mode=mode,
                    date_unconfirmed_ids=date_unconfirmed_ids,
                    n_blocked_sources=n_blocked,
                    n_post_cutoff=n_post_cutoff,
                    as_of_date=self._source_policy.as_of_date,
                )
                synth_quality = {
                    "passed": False,
                    "non_empty": True,
                    "not_prompt_echo": True,
                    "has_citation_tags": False,
                    "citations_traceable": False,
                    "citations_nonempty": bool(citations),
                    "failure_reason": (
                        f"quality gate failed: synthesis call error "
                        f"{type(e).__name__}: {str(e)[:300]}"
                    ),
                }
        else:
            md = render_markdown(
                query=query,
                claims=revised,
                citations=citations,
                metrics=metrics,
                mode=mode,
                date_unconfirmed_ids=date_unconfirmed_ids,
                n_blocked_sources=n_blocked,
                n_post_cutoff=n_post_cutoff,
                as_of_date=self._source_policy.as_of_date,
            )
        return VerifiedReport(
            markdown=md,
            html=html_str,
            claims=revised,
            metrics=metrics,
            verification_log=results,
            revision_log=revision_log,
            citations=citations,
            synthesis_quality=synth_quality,
        )


def _compute_metrics(claims: list[Claim]) -> VerificationMetrics:
    m = VerificationMetrics(total_claims=len(claims))
    for c in claims:
        if c.verification_status == "verified":
            m.verified += 1
        elif c.verification_status == "contradicted":
            m.contradicted += 1
        elif c.verification_status == "neutral":
            m.neutral += 1
        elif c.verification_status == "refetch_failed":
            m.refetch_failed += 1
        elif c.verification_status == "dropped":
            m.dropped += 1
    return m


def _safe_href(locator: str, title: str) -> str:
    """Render a citation locator as safe HTML: only http(s) gets a clickable
    <a>; everything else is inert escaped text."""
    e_title = _html.escape(title or locator, quote=True)
    if _SAFE_URL_RE.match(locator):
        return f'<a href="{_html.escape(locator, quote=True)}" rel="noopener noreferrer" target="_blank">{e_title}</a>'
    return f"<span>{e_title}</span>"


def render_markdown(
    *,
    query: str,
    claims: list[Claim],
    citations: list[Citation],
    metrics: VerificationMetrics,
    mode: str = "live",
    date_unconfirmed_ids: set[str] | None = None,
    n_blocked_sources: int = 0,
    n_post_cutoff: int = 0,
    as_of_date: str | None = None,
) -> str:
    lines: list[str] = []
    if mode == "fake":
        lines.append(
            "> ⚠️ 本报告由 fake 模式生成，使用预定义测试资料（example.com），"
            "仅供演示和测试，不代表真实研究结果。"
        )
        lines.append("")
    lines.append(f"# 调研报告：{query}")
    lines.append("")
    # Source-constraint audit line (DRB2): show that blocked/post-cutoff
    # material was filtered out, and state the as-of cutoff when one applies.
    if as_of_date or n_blocked_sources or n_post_cutoff:
        lines.append("> 来源约束审计：")
        if as_of_date:
            lines.append(f"> - 截止日期（as-of）：{as_of_date}")
        if n_blocked_sources:
            lines.append(f"> - 已过滤禁用来源引用：{n_blocked_sources}")
        if n_post_cutoff:
            lines.append(f"> - 已过滤截止日后材料：{n_post_cutoff}")
        lines.append("")
    lines.append(f"- 声明总数：{metrics.total_claims}")
    lines.append(f"- 已验证：{metrics.verified}")
    lines.append(f"- 矛盾：{metrics.contradicted}")
    lines.append(f"- 证据不足：{metrics.neutral}")
    lines.append(f"- 重取失败：{metrics.refetch_failed}")
    lines.append(f"- Citation Precision：{metrics.citation_precision:.2f}")
    lines.append(f"- Claim Coverage：{metrics.claim_coverage:.2f}")
    lines.append("")
    lines.append("## 已验证声明")
    lines.append("")
    verified = [c for c in claims if c.verification_status == "verified"]
    if not verified:
        lines.append("> 无已验证声明。")
    for c in verified:
        tags = ",".join(f"[{x}]" for x in c.citation_ids)
        lines.append(f"- {c.claim_text} {tags}")
    lines.append("")
    lines.append("## 存疑 / 矛盾声明")
    lines.append("")
    suspicious = [
        c for c in claims if c.verification_status in {"contradicted", "neutral", "refetch_failed"}
    ]
    if not suspicious:
        lines.append("> 无。")
    for c in suspicious:
        tags = ",".join(f"[{x}]" for x in c.citation_ids)
        lines.append(f"- {c.claim_text} {tags}  _[{c.verification_status}]_")
    lines.append("")
    lines.append("## 信息限制 / Limitations")
    lines.append("")
    limits = [c for c in claims if c.verification_status in {"refetch_failed", "dropped"}]
    if not limits:
        lines.append("> 无。")
    for c in limits:
        lines.append(f"- {c.claim_text}  _[{c.verification_status}]_")
    lines.append("")
    lines.append("## 引用")
    lines.append("")
    _unconfirmed = date_unconfirmed_ids or set()
    for cit in citations:
        note = "（日期未确认）" if cit.citation_id in _unconfirmed else ""
        lines.append(f"- [{cit.citation_id}] {cit.title or cit.locator} — {cit.locator}{note}")
    lines.append("")
    return "\n".join(lines)


def render_html(
    *,
    query: str,
    claims: list[Claim],
    citations: list[Citation],
    metrics: VerificationMetrics,
    mode: str = "live",
    date_unconfirmed_ids: set[str] | None = None,
) -> str:
    """Render escaped HTML. All user/source text goes through html.escape().
    Only http/https locators become clickable links; dangerous schemes are
    rendered as inert text."""

    def e(s: str) -> str:
        return _html.escape(s, quote=True)

    _unconfirmed = date_unconfirmed_ids or set()
    parts: list[str] = []
    parts.append("<!doctype html><meta charset='utf-8'><title>Report</title>")
    if mode == "fake":
        parts.append(
            '<div style="background:#fff3cd;border:1px solid #ffeaa7;padding:12px;'
            'margin:8px 0;border-radius:4px;color:#856404;">'
            "⚠️ 本报告由 fake 模式生成，使用预定义测试资料（example.com），"
            "仅供演示和测试，不代表真实研究结果。"
            "</div>"
        )
    parts.append(f"<h1>{e(query)}</h1>")
    parts.append("<ul>")
    parts.append(f"<li>total: {metrics.total_claims}</li>")
    parts.append(f"<li>verified: {metrics.verified}</li>")
    parts.append(f"<li>contradicted: {metrics.contradicted}</li>")
    parts.append(f"<li>citation_precision: {metrics.citation_precision:.2f}</li>")
    parts.append("</ul>")
    parts.append("<h2>Verified</h2><ul>")
    for c in claims:
        if c.verification_status != "verified":
            continue
        parts.append(f"<li>{e(c.claim_text)} {e(','.join(c.citation_ids))}</li>")
    parts.append("</ul><h2>Suspicious</h2><ul>")
    for c in claims:
        if c.verification_status in {"verified", "pending"}:
            continue
        parts.append(f"<li>{e(c.claim_text)} <small>{e(c.verification_status)}</small></li>")
    parts.append("</ul><h2>Limitations</h2><ul>")
    for c in claims:
        if c.verification_status not in {"refetch_failed", "dropped"}:
            continue
        parts.append(f"<li>{e(c.claim_text)} <small>{e(c.verification_status)}</small></li>")
    parts.append("</ul><h2>Citations</h2><ul>")
    for cit in citations:
        title = cit.title or cit.locator
        note = ' <small>（日期未确认）</small>' if cit.citation_id in _unconfirmed else ""
        parts.append(f"<li>[{e(cit.citation_id)}] {_safe_href(cit.locator, title)}{note}</li>")
    parts.append("</ul>")
    return "".join(parts)


__all__ = [
    "RevisionPolicy",
    "RevisionRecord",
    "VerifiedReport",
    "VerifiedReportBuilder",
    "check_report_quality",
    "facts_to_claims",
    "render_html",
    "render_markdown",
]
