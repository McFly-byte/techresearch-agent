"""Markdown report writer.

Contract:
- The writer ONLY receives (query, facts, citations). It has no other source
  of text. It emits a heading, a fact list, and a citation section.
- Every citation id referenced in the body MUST appear in `citations`.
  Enforced by `_CitationGuard`.
- The writer MUST NOT introduce citation ids that were not passed in.
"""

from __future__ import annotations

from core.exceptions import ToolError
from domain.models import Citation, Fact


class MarkdownReportWriter:
    name = "markdown"

    def render(self, *, query: str, facts: list[Fact], citations: list[Citation]) -> str:
        known = {c.citation_id for c in citations}
        # Defense in depth: reject facts that reference unknown citations.
        for f in facts:
            missing = [c for c in f.source_citation_ids if c not in known]
            if missing:
                raise ToolError(f"writer refuses fact {f.fact_id}: unknown citation ids {missing}")

        lines: list[str] = []
        lines.append(f"# 调研报告：{query}")
        lines.append("")
        lines.append(f"- 事实条数：{len(facts)}")
        lines.append(f"- 引用来源数：{len(citations)}")
        lines.append("")
        lines.append("## 要点")
        lines.append("")
        if not facts:
            lines.append("> 未从任何来源中提取到事实（见 errors 列表）。")
        for f in facts:
            tags = ",".join(f"[{c}]" for c in f.source_citation_ids)
            lines.append(f"- {f.claim} {tags}")
        lines.append("")
        lines.append("## 引用")
        lines.append("")
        for c in citations:
            lines.append(f"- [{c.citation_id}] {c.title or c.locator} — {c.locator}")
        lines.append("")
        return "\n".join(lines)


__all__ = ["MarkdownReportWriter"]
