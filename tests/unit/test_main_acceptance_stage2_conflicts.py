"""Same-id / different-payload merge conflict determinism (final stage-2 boundary).

The earlier "commutative" tests only covered DIFFERENT ids or completed-vs-failed.
These pin the remaining edge: two payloads sharing the same id but differing in
semantic fields must merge to ONE deterministic winner regardless of which worker
arrived first. The rule is a canonical serialization winner (lexicographically
smallest, after excluding non-semantic Pydantic timestamps), NOT first-arrival.

    - identical payload        -> idempotent, result unchanged.
    - same id, different body  -> canonical-min winner, order-independent.
    - completed vs failed       -> completed still wins (unchanged).
"""

from __future__ import annotations

from domain.models import Citation, Fact, SourceKind
from graph.state import SubTask, merge_citations, merge_facts, merge_subtasks

_FIXED_TS = "2026-01-01T00:00:00+00:00"


def _fact(fid: str, claim: str, *, source_task: str = "") -> Fact:
    return Fact(
        fact_id=fid,
        claim=claim,
        source_citation_ids=["c1"],
        extracted_at=_FIXED_TS,
        source_task_id=source_task,
    )


def _cit(cid: str, locator: str, *, title: str = "") -> Citation:
    return Citation(
        citation_id=cid,
        kind=SourceKind.WEB,
        locator=locator,
        title=title,
        fetched_at=_FIXED_TS,
    )


def _done(tid: str, summary: str) -> SubTask:
    return SubTask(
        task_id=tid,
        title=tid,
        description=tid,
        status="completed",  # type: ignore[arg-type]
        result_summary=summary,
    )


# ---------------------------------------------------------------------------
# Fact: same id, different payload
# ---------------------------------------------------------------------------
def test_merge_facts_same_id_different_payload_commutative() -> None:
    a = _fact("f1", "alpha claim")
    b = _fact("f1", "beta claim")
    forward = merge_facts([a], [b])
    backward = merge_facts([b], [a])
    assert [f.fact_id for f in forward] == [f.fact_id for f in backward]
    assert forward[0] == backward[0], "winner must not depend on arrival order"


def test_merge_facts_same_id_associative() -> None:
    a = _fact("f1", "alpha")
    b = _fact("f1", "beta")
    c = _fact("f1", "gamma")
    left_assoc = merge_facts(merge_facts([a], [b]), [c])
    right_assoc = merge_facts([a], merge_facts([b], [c]))
    assert left_assoc == right_assoc


def test_merge_facts_same_id_idempotent() -> None:
    a = _fact("f1", "alpha")
    b = _fact("f1", "beta")
    once = merge_facts([a], [b])
    twice = merge_facts(once, [b])
    assert twice == once


def test_merge_facts_identical_payload_idempotent() -> None:
    a = _fact("f1", "alpha", source_task="task_1")
    b = _fact("f1", "alpha", source_task="task_1")
    out = merge_facts([a], [b])
    assert len(out) == 1
    assert out[0] == a


def test_merge_facts_different_payload_still_kept_by_different_id() -> None:
    # sanity: different ids still both present and order-independent.
    a = _fact("f1", "alpha")
    b = _fact("f2", "beta")
    assert [x.fact_id for x in merge_facts([a], [b])] == [x.fact_id for x in merge_facts([b], [a])]


# ---------------------------------------------------------------------------
# Citation: same id, different payload
# ---------------------------------------------------------------------------
def test_merge_citations_same_id_different_payload_commutative() -> None:
    a = _cit("c1", "https://a.example", title="aaa")
    b = _cit("c1", "https://b.example", title="bbb")
    forward = merge_citations([a], [b])
    backward = merge_citations([b], [a])
    assert [c.citation_id for c in forward] == [c.citation_id for c in backward]
    assert forward[0] == backward[0]


def test_merge_citations_same_id_associative() -> None:
    a = _cit("c1", "https://a")
    b = _cit("c1", "https://b")
    c = _cit("c1", "https://c")
    assert merge_citations(merge_citations([a], [b]), [c]) == merge_citations(
        [a], merge_citations([b], [c])
    )


def test_merge_citations_same_id_idempotent() -> None:
    a = _cit("c1", "https://a")
    b = _cit("c1", "https://b")
    once = merge_citations([a], [b])
    assert merge_citations(once, [b]) == once


# ---------------------------------------------------------------------------
# SubTask: same id, same terminal status, different summary
# ---------------------------------------------------------------------------
def test_merge_subtasks_completed_same_id_different_summary_commutative() -> None:
    a = _done("task_1", "alpha summary")
    b = _done("task_1", "beta summary")
    forward = merge_subtasks([a], [b])
    backward = merge_subtasks([b], [a])
    assert forward[0] == backward[0], "terminal conflict winner must be deterministic"


def test_merge_subtasks_completed_same_id_associative() -> None:
    a = _done("task_1", "alpha")
    b = _done("task_1", "beta")
    c = _done("task_1", "gamma")
    assert merge_subtasks(merge_subtasks([a], [b]), [c]) == merge_subtasks(
        [a], merge_subtasks([b], [c])
    )


def test_merge_subtasks_completed_same_id_idempotent() -> None:
    a = _done("task_1", "alpha")
    b = _done("task_1", "beta")
    once = merge_subtasks([a], [b])
    assert merge_subtasks(once, [b]) == once


def test_merge_subtasks_completed_beats_failed_still_holds() -> None:
    failed = SubTask(task_id="task_1", title="x", description="x", status="failed")  # type: ignore[arg-type]
    done = _done("task_1", "ok")
    assert merge_subtasks([done], [failed])[0].status == "completed"
    assert merge_subtasks([failed], [done])[0].status == "completed"


def test_merge_subtasks_identical_completed_idempotent() -> None:
    a = _done("task_1", "alpha")
    b = _done("task_1", "alpha")
    out = merge_subtasks([a], [b])
    assert len(out) == 1
    assert out[0] == a
