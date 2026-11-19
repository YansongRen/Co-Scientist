"""Roundtrip tests for the storage repositories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from co_scientist import ids
from co_scientist.models import (
    Hypothesis,
    ResearchPlan,
    Review,
    ReviewScores,
    Session,
    SystemFeedback,
    Task,
)
from co_scientist.storage.repos import (
    feedback as fb_repo,
)
from co_scientist.storage.repos import (
    hypotheses as hyp_repo,
)
from co_scientist.storage.repos import (
    reviews as rev_repo,
)
from co_scientist.storage.repos import (
    sessions as sess_repo,
)
from co_scientist.storage.repos import (
    tasks as task_repo,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _make_session(conn, sid: str = "ses_test") -> Session:
    s = Session(
        id=sid, created_at=_now(), updated_at=_now(), status="running",
        research_goal="Test goal",
        research_plan=ResearchPlan(objective="x", preferences=["specificity"]),
        config_snapshot={"k": 1}, budget_tokens=10000, budget_usd=1.0,
    )
    await sess_repo.insert(conn, s)
    return s


@pytest.mark.asyncio
async def test_sessions_roundtrip(conn) -> None:
    s = await _make_session(conn)
    s2 = await sess_repo.fetch(conn, s.id)
    assert s2 is not None
    assert s2.research_goal == s.research_goal
    assert s2.research_plan.objective == "x"
    assert s2.research_plan.preferences == ["specificity"]
    assert s2.config_snapshot == {"k": 1}


@pytest.mark.asyncio
async def test_hypothesis_insert_or_ignore_dedupes_on_deterministic_id(conn) -> None:
    s = await _make_session(conn)
    statement = "Hypothesis: X causes Y via Z."
    hid = ids.hypothesis_id(s.id, "generation/literature", statement)
    h = Hypothesis(
        id=hid, session_id=s.id, created_at=_now(),
        created_by="generation", strategy="literature",
        title="t", summary=statement, full_text="long",
        artifact_path=f"artifacts/{s.id}/hypotheses/{hid}.json",
        state="draft",
    )
    assert await hyp_repo.insert(conn, h) is True
    # Same statement → same id → INSERT OR IGNORE returns False
    h2 = Hypothesis(**{**h.model_dump(), "title": "different title"})
    assert await hyp_repo.insert(conn, h2) is False
    fetched = await hyp_repo.fetch(conn, hid)
    assert fetched is not None
    assert fetched.title == "t"  # original wins


@pytest.mark.asyncio
async def test_init_tournament_only_runs_once(conn) -> None:
    s = await _make_session(conn)
    hid = ids.hypothesis_id(s.id, "generation/literature", "h")
    await hyp_repo.insert(conn, Hypothesis(
        id=hid, session_id=s.id, created_at=_now(),
        created_by="generation", strategy="literature",
        title="t", summary="s", full_text="f",
        artifact_path=f"artifacts/{s.id}/hypotheses/{hid}.json", state="reviewed",
    ))
    assert await hyp_repo.init_tournament(conn, hid, initial_elo=1200) is True
    # second call must be a no-op (Elo already set)
    assert await hyp_repo.init_tournament(conn, hid, initial_elo=9999) is False
    h = await hyp_repo.fetch(conn, hid)
    assert h is not None and h.elo == 1200


@pytest.mark.asyncio
async def test_set_state_if_only_applies_when_expected(conn) -> None:
    """set_state_if must only transition from one of the expected source states."""
    s = await _make_session(conn)
    hid = ids.hypothesis_id(s.id, "generation/literature", "h-state")
    await hyp_repo.insert(conn, Hypothesis(
        id=hid, session_id=s.id, created_at=_now(),
        created_by="generation", strategy="literature",
        title="t", summary="s", full_text="f",
        artifact_path=f"artifacts/{s.id}/hypotheses/{hid}.json", state="draft",
    ))
    # draft → reviewed: allowed
    applied = await hyp_repo.set_state_if(
        conn, hid, new_state="reviewed", expected_states=("draft",)
    )
    assert applied is True

    # Promote past reflection into the tournament.
    await hyp_repo.init_tournament(conn, hid, initial_elo=1200)

    # Reflection re-fires: must NOT drag in_tournament → reviewed.
    applied2 = await hyp_repo.set_state_if(
        conn, hid, new_state="reviewed", expected_states=("draft",)
    )
    assert applied2 is False
    h = await hyp_repo.fetch(conn, hid)
    assert h is not None and h.state == "in_tournament"


@pytest.mark.asyncio
async def test_review_id_iteration_collision_blocked(conn) -> None:
    s = await _make_session(conn)
    hid = ids.hypothesis_id(s.id, "generation/literature", "h")
    await hyp_repo.insert(conn, Hypothesis(
        id=hid, session_id=s.id, created_at=_now(),
        created_by="generation", strategy="literature",
        title="t", summary="s", full_text="f",
        artifact_path=f"artifacts/{s.id}/hypotheses/{hid}.json", state="draft",
    ))
    rid = ids.review_id(hid, "full", iteration=0)
    r = Review(
        id=rid, hypothesis_id=hid, session_id=s.id, created_at=_now(),
        kind="full", verdict="missing_piece",
        scores=ReviewScores(novelty=0.7, correctness=0.6, testability=0.5),
        body="ok", artifact_path=f"artifacts/{s.id}/reviews/{rid}.json",
    )
    assert await rev_repo.insert(conn, r) is True
    assert await rev_repo.insert(conn, r) is False   # idempotent


@pytest.mark.asyncio
async def test_task_queue_claim_and_idempotency(conn) -> None:
    s = await _make_session(conn)
    t = Task(
        id=ids.task_id(), session_id=s.id, created_at=_now(),
        agent="reflection", action="ReviewHypothesis", target_id="hyp_z",
        payload={}, priority=100, status="pending",
        idempotency_key="hyp_z::review::full",
    )
    assert await task_repo.enqueue(conn, t) is True
    assert await task_repo.enqueue(conn, t) is False   # idempotency_key collision

    claimed = await task_repo.claim_one(conn, s.id, "w1", lease_seconds=60)
    assert claimed is not None
    assert claimed.id == t.id
    assert claimed.status == "leased"

    # nothing else to claim
    again = await task_repo.claim_one(conn, s.id, "w1", lease_seconds=60)
    assert again is None


@pytest.mark.asyncio
async def test_feedback_targeting(conn) -> None:
    s = await _make_session(conn)
    await fb_repo.insert(conn, SystemFeedback(
        id=ids.feedback_id(), session_id=s.id, created_at=_now(),
        source="human", kind="directive", target_id=None,
        text="focus on insulin signaling", active=True,
    ))
    await fb_repo.insert(conn, SystemFeedback(
        id=ids.feedback_id(), session_id=s.id, created_at=_now(),
        source="human", kind="pin", target_id="hyp_keep_me",
        text="pinned", active=True,
    ))
    global_only = await fb_repo.active_for_session(conn, s.id)
    assert len(global_only) == 1
    assert global_only[0].kind == "directive"

    targeted = await fb_repo.active_for_session(conn, s.id, target_id="hyp_keep_me")
    assert {f.kind for f in targeted} == {"directive", "pin"}

    await fb_repo.insert(conn, SystemFeedback(
        id=ids.feedback_id(), session_id=s.id, created_at=_now(),
        source="human", kind="preference", target_id=None,
        text="prefer cheaper assays", active=True,
    ))
    await fb_repo.insert(conn, SystemFeedback(
        id=ids.feedback_id(), session_id=s.id, created_at=_now(),
        source="human", kind="rejection", target_id="hyp_keep_me",
        text="", active=True,
    ))
    t0 = _now()
    await fb_repo.insert(conn, SystemFeedback(
        id=ids.feedback_id(), session_id=s.id, created_at=t0,
        source="meta_review", kind="system_feedback", target_id=None,
        text="older meta", active=True,
    ))
    await fb_repo.insert(conn, SystemFeedback(
        id=ids.feedback_id(), session_id=s.id, created_at=t0 + timedelta(seconds=1),
        source="meta_review", kind="system_feedback", target_id=None,
        text="latest meta", active=True,
    ))
    await fb_repo.insert(conn, SystemFeedback(
        id=ids.feedback_id(), session_id=s.id, created_at=_now(),
        source="human", kind="directive", target_id=None,
        text="inactive note", active=False,
    ))

    global_prompt = await fb_repo.compose_prompt(conn, s.id)
    assert "Researcher directive: focus on insulin signaling" in global_prompt
    assert "Researcher preference: prefer cheaper assays" in global_prompt
    assert "Meta-review: latest meta" in global_prompt
    assert "older meta" not in global_prompt
    assert "inactive note" not in global_prompt
    assert "Researcher pin note" not in global_prompt
    assert global_prompt.index("Researcher") < global_prompt.index("Meta-review")

    targeted_prompt = await fb_repo.compose_prompt(conn, s.id, target_id="hyp_keep_me")
    assert "Researcher pin note: pinned" in targeted_prompt
    assert "Researcher rejection note" not in targeted_prompt
    assert "inactive note" not in targeted_prompt
