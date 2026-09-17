"""Unit tests for state reducers and concurrency semantics."""

from __future__ import annotations

from domain.models import Citation, Fact, SourceKind
from graph.state import SubTask, merge_citations, merge_errors, merge_facts, merge_subtasks


def _t(tid: str, status: str = "pending", deps: list[str] | None = None) -> SubTask:
    return SubTask(
        task_id=tid,
        title=tid,
        description=tid,
        depends_on=deps or [],
        status=status,  # type: ignore[arg-type]
    )


def test_merge_subtasks_keeps_terminal_lock():  # type: ignore[no-untyped-def]
    left = [_t("task_1", "completed")]
    right = [_t("task_1", "pending")]  # bogus re-dispatch
    out = merge_subtasks(left, right)
    assert out[0].status == "completed"


def test_merge_subtasks_idempotent_completion():  # type: ignore[no-untyped-def]
    a = _t("task_1", "completed", deps=[])
    a = a.model_copy(update={"result_summary": "done v1"})
    b = _t("task_1", "completed")
    b = b.model_copy(update={"result_summary": "done v2"})
    out = merge_subtasks([a], [b])
    assert out[0].result_summary == "done v1"  # first terminal wins


def test_merge_subtasks_second_update_overwrites_nonterminal():  # type: ignore[no-untyped-def]
    left = [_t("task_1", "running")]
    right = [_t("task_1", "completed")]
    out = merge_subtasks(left, right)
    assert out[0].status == "completed"


def test_merge_subtasks_preserves_unknown():  # type: ignore[no-untyped-def]
    left = [_t("task_1", "pending")]
    right = [_t("task_2", "pending")]
    out = merge_subtasks(left, right)
    assert {t.task_id for t in out} == {"task_1", "task_2"}


def test_merge_facts_dedup_by_id():  # type: ignore[no-untyped-def]
    def f(fid: str) -> Fact:
        return Fact(fact_id=fid, claim="x", source_citation_ids=["c1"])

    out = merge_facts([f("f1")], [f("f1"), f("f2")])
    assert [x.fact_id for x in out] == ["f1", "f2"]


def test_merge_citations_dedup():  # type: ignore[no-untyped-def]
    def c(cid: str) -> Citation:
        return Citation(citation_id=cid, kind=SourceKind.WEB, locator=f"https://{cid}")

    out = merge_citations([c("c1")], [c("c1"), c("c2")])
    assert [x.citation_id for x in out] == ["c1", "c2"]


def test_merge_errors_dedup():  # type: ignore[no-untyped-def]
    out = merge_errors(["e1"], ["e1", "e2", ""])
    assert out == ["e1", "e2"]


def test_merge_deterministic_order_independent_of_worker_order():  # type: ignore[no-untyped-def]
    # Two workers finishing in either order must produce the same final state.
    a1 = _t("task_1", "completed")
    a2 = _t("task_2", "completed")
    s1 = merge_subtasks([a1], [a2])
    s2 = merge_subtasks([a2], [a1])
    assert [t.task_id for t in s1] == [t.task_id for t in s2]


def test_merge_subtasks_collapses_duplicate_task_id_in_right():  # type: ignore[no-untyped-def]
    # A planner / re-delivery that emits the same task_id twice must not create
    # two subtask rows; the reducer keys by task_id and keeps one entry.
    left = []
    right = [
        _t("task_1", "pending"),
        _t("task_1", "running"),  # duplicate delivery of the same task_id
        _t("task_2", "pending"),
    ]
    out = merge_subtasks(left, right)
    assert [t.task_id for t in out] == ["task_1", "task_2"]
    # The later (non-terminal) update wins for the duplicated id.
    t1 = next(t for t in out if t.task_id == "task_1")
    assert t1.status == "running"


def test_merge_subtasks_duplicate_terminal_delivery_is_idempotent():  # type: ignore[no-untyped-def]
    # Same task_id delivered as completed twice in one batch: first terminal
    # wins, no resurrection, no duplicate row.
    a = _t("task_1", "completed")
    a = a.model_copy(update={"result_summary": "done v1"})
    b = _t("task_1", "completed")
    b = b.model_copy(update={"result_summary": "done v2"})
    out = merge_subtasks([], [a, b])
    assert len(out) == 1
    assert out[0].result_summary == "done v1"
