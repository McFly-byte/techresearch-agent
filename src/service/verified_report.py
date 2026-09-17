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

import html as _html
import re
from dataclasses import dataclass, field

from core.exceptions import ToolError
from domain.models import Citation, Fact
from domain.verification import (
    Claim,
    VerificationMetrics,
    VerificationResult,
)
from service.verifier import CitationVerifier

_SAFE_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


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
    ) -> None:
        self._verifier = verifier or CitationVerifier()
        self._policy = revision_policy or RevisionPolicy()

    async def build(
        self,
        *,
        query: str,
        facts: list[Fact],
        citations: list[Citation],
        mode: str = "live",
    ) -> VerifiedReport:
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
        md = render_markdown(
            query=query, claims=revised, citations=citations, metrics=metrics, mode=mode
        )
        html_str = render_html(
            query=query, claims=revised, citations=citations, metrics=metrics, mode=mode
        )
        return VerifiedReport(
            markdown=md,
            html=html_str,
            claims=revised,
            metrics=metrics,
            verification_log=results,
            revision_log=revision_log,
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
    for cit in citations:
        lines.append(f"- [{cit.citation_id}] {cit.title or cit.locator} — {cit.locator}")
    lines.append("")
    return "\n".join(lines)


def render_html(
    *,
    query: str,
    claims: list[Claim],
    citations: list[Citation],
    metrics: VerificationMetrics,
    mode: str = "live",
) -> str:
    """Render escaped HTML. All user/source text goes through html.escape().
    Only http/https locators become clickable links; dangerous schemes are
    rendered as inert text."""

    def e(s: str) -> str:
        return _html.escape(s, quote=True)

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
        parts.append(f"<li>[{e(cit.citation_id)}] {_safe_href(cit.locator, title)}</li>")
    parts.append("</ul>")
    return "".join(parts)


__all__ = [
    "RevisionPolicy",
    "RevisionRecord",
    "VerifiedReport",
    "VerifiedReportBuilder",
    "facts_to_claims",
    "render_html",
    "render_markdown",
]
