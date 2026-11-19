"""Offline quality checklist. No hosted model calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from co_scientist.evals.scorecard import (
    FIXED_GOAL,
    PROTOCOL,
    apply_protocol,
    later_outranks_early,
    session_scorecard,
)
from co_scientist.models import Hypothesis, ResearchPlan, Session, SystemFeedback, Task, TournamentMatch
from co_scientist.storage.repos import feedback as fb_repo
from co_scientist.storage.repos import hypotheses, sessions, tasks, tournaments


def test_apply_protocol_isolates_data_dir_and_clears_authors(tmp_path):
    from co_scientist.config import Config, GenerationCfg, RunCfg, StorageCfg

    cfg = Config(
        run=RunCfg(research_mode="standard", budget_usd=1.0, budget_tokens=500000),
        storage=StorageCfg(data_dir="data"),
        generation=GenerationCfg(authors=["kimi", "glm", "session"]),
    )
    overlay = apply_protocol(cfg, tmp_path / "protocol")
    assert overlay.storage.data_dir.endswith("protocol")
    assert overlay.run.research_mode == PROTOCOL["research_mode"]
    assert overlay.run.budget_usd == PROTOCOL["budget_usd"]
    assert overlay.run.budget_tokens == PROTOCOL["budget_tokens"]
    assert overlay.run.wall_clock_seconds == PROTOCOL["wall_clock_seconds"]
    assert overlay.generation.authors == []
    assert cfg.run.research_mode == "standard"
    assert cfg.generation.authors == ["kimi", "glm", "session"]
    assert cfg.storage.data_dir == "data"


def test_later_half_outranks_when_newer_elo_is_higher():
    now = datetime.now(UTC)
    early = Hypothesis(
        id="h0", session_id="s", created_at=now, created_by="generation",
        strategy="literature", title="a", summary="a", full_text="a",
        artifact_path="a.json", elo=1200, state="in_tournament",
    )
    late = Hypothesis(
        id="h1", session_id="s", created_at=now + timedelta(seconds=1),
        created_by="evolution", strategy="simplify", title="b", summary="b",
        full_text="b", artifact_path="b.json", elo=1300, state="in_tournament",
    )
    assert later_outranks_early([early, late]) is True
    late.elo = 1100
    assert later_outranks_early([early, late]) is False
    assert later_outranks_early([early]) is None


@pytest.mark.asyncio
async def test_scorecard_reads_persisted_session_without_models(tmp_cfg, conn):
    now = datetime.now(UTC)
    session = Session(
        id="card-session", created_at=now, updated_at=now, status="done",
        research_goal=FIXED_GOAL, research_plan=ResearchPlan(objective=FIXED_GOAL),
        config_snapshot={
            "termination": {"reason": "idle", "hypothesis_count": 2, "output": "complete"},
            "research_execution": {"literature_brief": "artifacts/x.json"},
        },
        budget_tokens=100000, budget_usd=2, budget_used_tokens=900, budget_used_usd=0.04,
    )
    await sessions.insert(conn, session)
    for i, elo in enumerate((1210.0, 1280.0)):
        await hypotheses.insert(conn, Hypothesis(
            id=f"card-h{i}", session_id=session.id,
            created_at=now + timedelta(seconds=i), created_by="generation",
            strategy="literature", title=f"h{i}", summary=f"h{i}", full_text=f"h{i}",
            artifact_path=f"h{i}.json", elo=elo, matches_played=1, state="in_tournament",
        ))
    await tournaments.insert_match(conn, TournamentMatch(
        id="card-m0", session_id=session.id, created_at=now,
        hyp_a="card-h0", hyp_b="card-h1", mode="pairwise", winner="b",
        elo_a_before=1200, elo_b_before=1200,
    ))
    await tasks.enqueue(conn, Task(
        id="card-evo", session_id=session.id, created_at=now,
        agent="evolution", action="EvolveTopHypotheses", status="done",
    ))
    await fb_repo.insert(conn, SystemFeedback(
        id="card-fb", session_id=session.id, created_at=now,
        source="meta_review", kind="system_feedback", text="focus on noise",
        active=True,
    ))
    card = await session_scorecard(conn, session.id)
    assert card["hypothesis_count"] == 2
    assert card["match_count"] == 1
    assert card["evolution_done"] == 1
    assert card["meta_feedback"] == 1
    assert card["later_outranks_early"] is True
    assert card["termination"]["reason"] == "idle"
    assert card["human_score"] is None
    assert card["literature_status"] == "recorded"
    assert "not experimental effect" in card["note"]
    assert await session_scorecard(conn, "missing") is None
