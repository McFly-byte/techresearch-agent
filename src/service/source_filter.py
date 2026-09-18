"""Shared source-constraint policy for the research pipeline.

DRB2 ships two research-time rules that the system must enforce (these are NOT
judge signals — they already appear verbatim in the prompt's ``**important**``
block and as-of sentence):

1. **Blocked sources**: specific articles (exact URL), their hosting domains,
   and their titles must never be fetched or cited.
2. **As-of cutoff**: material published *after* the stated cutoff year must not
   appear in the report (e.g. task7 "截至2021年" -> drop anything dated 2022+).

``SourcePolicy`` is the single object that both the worker (search/fetch gate)
and the report builder (final citation gate) consult. It is a no-op when empty
so existing tasks without constraints behave exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from domain.models import Citation, SearchResult

# Years we treat as plausible publication years when scanning citation text.
# 1980..2049 covers the realistic range; we deliberately do NOT match every
# 4-digit number (e.g. 1000, 9999) to avoid false positives.
_YEAR_RE = re.compile(r"(19[89]\d|20[0-4]\d)")

# Unicode -> ASCII normalization for title matching. Search engines and publishers
# often use curly apostrophes/quotes/hyphens, while the benchmark blocked titles
# may use straight ASCII. Normalize both sides before substring comparison so a
# blocked article on a mirror site (semanticscholar, etc.) is still caught.
_UNICODE_FIXES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u00a0": " ", "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
})


def _normalize_title(text: str) -> str:
    """Lowercase + Unicode->ASCII normalize for blocked-title matching."""
    return (text or "").strip().lower().translate(_UNICODE_FIXES)


def _normalize_url_key(url: str) -> str:
    """Produce a comparison key for exact-URL blocking.

    Lowercases scheme+netloc, strips a leading ``www.`` and a trailing slash.
    Path/query are kept as-is (case-sensitive) — exact blocked URLs are pinned
    by the benchmark, so we do not fuzzy-match their paths.
    """
    try:
        p = urlparse(url.strip())
    except Exception:  # noqa: BLE001
        return url.strip().lower()
    scheme = (p.scheme or "").lower()
    netloc = (p.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = p.path or ""
    if path.endswith("/"):
        path = path[:-1]
    query = f"?{p.query}" if p.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def _normalize_domain(url: str) -> str:
    """Lowercase, portless, www-stripped host for suffix matching."""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    if not netloc:
        return ""
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


@dataclass(frozen=True)
class SourcePolicy:
    """Immutable set of source constraints for one research task.

    All fields default to empty / None, so a no-constraint policy is a no-op.
    """

    blocked_urls: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    blocked_titles: list[str] = field(default_factory=list)
    as_of_date: str | None = None  # cutoff year as a string, e.g. "2021"

    def __post_init__(self) -> None:
        # Pre-compute normalized keys once so every lookup is cheap.
        object.__setattr__(self, "_url_keys", [_normalize_url_key(u) for u in self.blocked_urls])
        object.__setattr__(self, "_domains", [d.lower() for d in self.blocked_domains if d])
        object.__setattr__(self, "_titles", [_normalize_title(t) for t in self.blocked_titles if t])
        as_year: int | None = None
        if self.as_of_date and self.as_of_date.isdigit():
            as_year = int(self.as_of_date)
        object.__setattr__(self, "_as_year", as_year)

    # injected pre-computed caches (never serialized)
    _url_keys: list[str] = field(default_factory=list, repr=False)
    _domains: list[str] = field(default_factory=list, repr=False)
    _titles: list[str] = field(default_factory=list, repr=False)
    _as_year: int | None = None

    def is_empty(self) -> bool:
        return not (
            self.blocked_urls or self.blocked_domains or self.blocked_titles or self.as_of_date
        )

    # --- URL gates ----------------------------------------------------------
    def is_blocked_url(self, url: str) -> bool:
        """True if ``url`` matches a blocked exact URL or a blocked domain."""
        key = _normalize_url_key(url)
        if key in self._url_keys:
            return True
        dom = _normalize_domain(url)
        if not dom:
            return False
        for blocked_dom in self._domains:
            if dom == blocked_dom or dom.endswith("." + blocked_dom):
                return True
        return False

    def is_blocked_title(self, title: str) -> bool:
        """True if ``title`` overlaps a blocked article title (substring,
        case-insensitive, Unicode-normalized). Either direction is checked so
        truncated search snippets still match."""
        t = _normalize_title(title)
        if not t:
            return False
        for blocked in self._titles:
            if not blocked:
                continue
            if blocked in t or t in blocked:
                return True
        return False

    # --- Search-result gate -------------------------------------------------
    def filter_search_hits(
        self, hits: list[SearchResult]
    ) -> tuple[list[SearchResult], int]:
        """Drop blocked search hits. Returns (kept_hits, dropped_count)."""
        kept: list[SearchResult] = []
        dropped = 0
        for h in hits:
            if self.is_blocked_url(h.url) or self.is_blocked_title(h.title):
                dropped += 1
                continue
            kept.append(h)
        return kept, dropped

    # --- Citation gate (final report) ---------------------------------------
    def citation_decision(self, citation: Citation) -> tuple[bool, str]:
        """Decide whether a citation may appear in the final report.

        Returns ``(keep, reason)``. ``reason`` is "" when kept, or one of
        ``blocked_url`` / ``blocked_domain`` / ``blocked_title`` /
        ``post_cutoff`` when dropped. A citation with no detectable year and
        no blocked match is kept (the builder may still mark it
        "日期未确认").
        """
        if self.is_blocked_url(citation.locator):
            return False, "blocked_url"
        if self.is_blocked_title(citation.title):
            return False, "blocked_title"
        if self._as_year is not None:
            year = _max_year(citation.title, citation.snippet)
            if year is not None and year > self._as_year:
                return False, "post_cutoff"
        return True, ""

    def has_as_of(self) -> bool:
        return self._as_year is not None

    def is_date_unconfirmed(self, citation: Citation) -> bool:
        """True when we could not detect any year and an as-of cutoff is set."""
        if self._as_year is None:
            return False
        return _max_year(citation.title, citation.snippet) is None


def _max_year(*texts: str) -> int | None:
    """Largest plausible publication year found in the given texts."""
    years: list[int] = []
    for t in texts:
        if not t:
            continue
        for m in _YEAR_RE.finditer(t):
            years.append(int(m.group(1)))
    return max(years) if years else None


__all__ = ["SourcePolicy", "_max_year", "_normalize_domain", "_normalize_url_key"]
