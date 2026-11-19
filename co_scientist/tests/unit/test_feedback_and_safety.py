"""Researcher feedback in prompts, Meta-review idle trigger, and safety gating."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.generation import GenerationAgent
from co_scientist.agents.metareview import MetaReviewAgent
from co_scientist.agents.supervisor import Supervisor
from co_scientist.models import Hypothesis, ResearchPlan, Session, SystemFeedback, TournamentMatch
from co_scientist.orchestrator.research_policy import meta_feedback_due
from co_scientist.safety.classifier import ClassifierResult, SafetyBlocked
from co_scientist.storage.repos import events as events_repo
from co_scientist.storage.repos import feedback as fb_repo
from co_scientist.storage.repos import hypotheses, sessions, tournaments
from co_scientist.tests.unit.test_hypothesis_diversity import RecordingLLM, deps, seed, task
from co_scientist.tools.registry import ToolRegistry


def test_meta_feedback_due_first_pass_uses_pool_not_fifty() -> None:
    assert not meta_feedback_due(0, 0, pool_size=3, every=10)
    assert not meta_feedback_due(1, 0, pool_size=5, every=10)
    assert meta_feedback_due(3, 0, pool_size=3, every=10)
    assert meta_feedback_due(2, 0, pool_size=2, every=10)
    assert not meta_feedback_due(2, 0, pool_size=10, every=10)
    assert meta_feedback_due(10, 0, pool_size=10, every=10)
    assert not meta_feedback_due(10, 1, pool_size=10, every=10)
    assert meta_feedback_due(20, 1, pool_size=10, every=10)
    assert meta_feedback_due(3, 0, pool_size=3, every=50)
    assert not meta_feedback_due(3, 1, pool_size=3, every=50)
    assert not meta_feedback_due(10, 0, pool_size=3, every=0)


async def _standard_pool(conn, *, matches: int, n: int = 3, sid="feedback-session"):
    now = datetime.now(UTC)
    session = Session(
        id=sid, created_at=now, updated_at=now, status="running",
        research_goal="Compare two measurement explanations",
        research_plan=ResearchPlan(objective="Compare two measurement explanations"),
        config_snapshot={}, budget_tokens=100000, budget_usd=1,
    )
    await sessions.insert(conn, session)
    ids = [f"{sid}-{i}" for i in range(n)]
    for hid in ids:
        await hypotheses.insert(conn, Hypothesis(
            id=hid, session_id=sid, created_at=now, created_by="generation",
            strategy="literature", title=hid, summary=hid, full_text=hid,
            artifact_path=f"artifacts/{hid}.json", state="in_tournament",
        ))
    for index in range(matches):
        a, b = ids[index % n], ids[(index + 1) % n]
        await tournaments.insert_match(conn, TournamentMatch(
            id=f"{sid}-m{index}", session_id=sid, created_at=now,
            hyp_a=a, hyp_b=b, mode="pairwise", winner="a",
            elo_a_before=1200, elo_b_before=1200,
        ))
    return session


@pytest.mark.asyncio
async def test_standard_idle_enqueues_meta_review_on_small_tournament(tmp_cfg, conn):
    tmp_cfg.metareview.feedback_every_matches = 50
    session = await _standard_pool(conn, matches=3)
    await Supervisor(tmp_cfg)._decide_next_steps(conn, session)
    async with conn.execute("SELECT agent,action FROM tasks") as cur:
        rows = [(r[0], r[1]) for r in await cur.fetchall()]
    assert ("metareview", "GenerateSystemFeedback") in rows


@pytest.mark.asyncio
async def test_standard_idle_can_disable_periodic_meta_review(tmp_cfg, conn):
    tmp_cfg.metareview.feedback_every_matches = 0
    session = await _standard_pool(conn, matches=12, sid="feedback-off")
    await Supervisor(tmp_cfg)._decide_next_steps(conn, session)
    async with conn.execute("SELECT agent FROM tasks") as cur:
        assert "metareview" not in [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_human_directive_drives_feedback_strategy_without_meta(tmp_cfg, conn):
    await seed(conn)
    tmp_cfg.embeddings.provider = "hash"
    await fb_repo.insert(conn, SystemFeedback(
        id="fb-directive", session_id="diversity", created_at=datetime.now(UTC),
        source="human", kind="directive", text="Focus on measurement noise",
        active=True,
    ))
    llm = RecordingLLM()
    result = await GenerationAgent(deps(tmp_cfg, conn, llm)).execute(task(strategy="feedback_driven"))
    assert result.extra["effective_strategy"] == "feedback_driven"
    prompt = llm.specs[-1][0].user_blocks[0].text
    assert "Researcher directive: Focus on measurement noise" in prompt


@pytest.mark.asyncio
async def test_blocked_goal_fails_session(tmp_cfg, conn, monkeypatch):
    tmp_cfg.safety.enable_classifier = True
    now = datetime.now(UTC)
    session = Session(
        id="safe-goal", created_at=now, updated_at=now, status="running",
        research_goal="synthesize a banned toxin",
        research_plan=ResearchPlan(objective="synthesize a banned toxin"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1,
    )
    await sessions.insert(conn, session)

    async def classify(self, text, **kwargs):
        assert "banned toxin" in text
        return ClassifierResult(categories=["cbrn"], confidence=0.99, rationale="block")

    monkeypatch.setattr("co_scientist.safety.classifier.SafetyClassifier.classify", classify)
    deps_ = AgentDeps(tmp_cfg, conn, None, ToolRegistry(tmp_cfg).discover())
    with pytest.raises(SafetyBlocked):
        await Supervisor(tmp_cfg)._enforce_goal_safety(deps_, session, None)
    saved = await sessions.fetch(conn, session.id)
    assert saved.status == "failed"
    names = {row["event"] for row in await events_repo.recent(conn, session.id)}
    assert "safety_assessment" in names
    assert "safety_blocked" in names


@pytest.mark.asyncio
async def test_blocked_hypothesis_is_not_inserted(tmp_cfg, conn, monkeypatch):
    await seed(conn)
    tmp_cfg.safety.enable_classifier = True
    tmp_cfg.embeddings.provider = "hash"

    async def classify(self, text, **kwargs):
        return ClassifierResult(categories=["weapons"], confidence=1.0, rationale="block")

    monkeypatch.setattr("co_scientist.safety.classifier.SafetyClassifier.classify", classify)
    hid, inserted = await GenerationAgent(deps(tmp_cfg, conn))._persist(
        "diversity", {"statement": "A blocked claim"}, strategy="literature",
    )
    assert hid is None and not inserted
    assert await hypotheses.list_for_session(conn, "diversity") == []


@pytest.mark.asyncio
async def test_quarantined_hypothesis_skips_review_follow_up(tmp_cfg, conn, monkeypatch):
    await seed(conn)
    tmp_cfg.safety.enable_classifier = True
    tmp_cfg.embeddings.provider = "hash"

    async def classify(self, text, **kwargs):
        return ClassifierResult(categories=["dual_use_bio"], confidence=0.85, rationale="flag")

    monkeypatch.setattr("co_scientist.safety.classifier.SafetyClassifier.classify", classify)
    result = await GenerationAgent(deps(tmp_cfg, conn, RecordingLLM())).execute(
        task(strategy="literature"),
    )
    assert result.kind == "noop"
    assert result.extra["reason"] == "hypothesis quarantined"
    pool = await hypotheses.list_for_session(conn, "diversity")
    assert len(pool) == 1 and pool[0].state == "quarantined"


@pytest.mark.asyncio
async def test_final_overview_safety_skipped_when_classifier_off(tmp_cfg, conn):
    now = datetime.now(UTC)
    session = Session(
        id="ov-skip", created_at=now, updated_at=now, status="running",
        research_goal="g", research_plan=ResearchPlan(objective="g"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1,
    )
    await sessions.insert(conn, session)
    agent = MetaReviewAgent(AgentDeps(tmp_cfg, conn, None, ToolRegistry(tmp_cfg).discover()))
    text, action = await agent._publish_overview_safety(session, "# Draft")
    assert action == "skipped" and text == "# Draft"


@pytest.mark.asyncio
async def test_final_overview_block_replaces_draft(tmp_cfg, conn, monkeypatch):
    tmp_cfg.safety.enable_classifier = True
    now = datetime.now(UTC)
    session = Session(
        id="ov-block", created_at=now, updated_at=now, status="running",
        research_goal="g", research_plan=ResearchPlan(objective="g"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1,
    )
    await sessions.insert(conn, session)

    async def classify(self, text, **kwargs):
        assert "secret route" in text
        return ClassifierResult(categories=["cbrn"], confidence=0.99, rationale="block")

    monkeypatch.setattr("co_scientist.safety.classifier.SafetyClassifier.classify", classify)
    agent = MetaReviewAgent(AgentDeps(tmp_cfg, conn, None, ToolRegistry(tmp_cfg).discover()))
    text, action = await agent._publish_overview_safety(session, "# Draft\nsecret route")
    assert action == "block"
    assert "not published" in text
    assert "secret route" not in text
    names = {row["event"] for row in await events_repo.recent(conn, session.id)}
    assert "safety_assessment" in names and "safety_blocked" in names


@pytest.mark.asyncio
async def test_final_overview_warn_keeps_draft_with_note(tmp_cfg, conn, monkeypatch):
    tmp_cfg.safety.enable_classifier = True
    now = datetime.now(UTC)
    session = Session(
        id="ov-warn", created_at=now, updated_at=now, status="running",
        research_goal="g", research_plan=ResearchPlan(objective="g"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1,
    )
    await sessions.insert(conn, session)

    async def classify(self, text, **kwargs):
        return ClassifierResult(categories=["dual_use_bio"], confidence=0.4, rationale="warn")

    monkeypatch.setattr("co_scientist.safety.classifier.SafetyClassifier.classify", classify)
    agent = MetaReviewAgent(AgentDeps(tmp_cfg, conn, None, ToolRegistry(tmp_cfg).discover()))
    text, action = await agent._publish_overview_safety(session, "# Draft")
    assert action == "warn"
    assert "# Draft" in text
    assert "Safety note" in text
