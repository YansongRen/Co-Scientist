"""Reflection chain, similar-hypothesis pairing, and strategy bias."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.reflection import ReflectionAgent
from co_scientist.agents.supervisor import Supervisor
from co_scientist.llm.types import LLMResponse, SynthMessage, ToolUseBlock
from co_scientist.models import Hypothesis, ResearchPlan, Session, Task, TaskResult
from co_scientist.orchestrator.researcher import submit_researcher_hypothesis
from co_scientist.orchestrator.strategy_effect import prefer_effective
from co_scientist.storage.repos import hypotheses, reviews, sessions, tasks
from co_scientist.tools.registry import ToolRegistry


def _response(verdict):
    block = ToolUseBlock(name="record_review", input={
        "verdict": verdict, "kind": "ignored", "evidence": [],
        "novelty": 0.4, "correctness": 0.4, "testability": 0.4, "feasibility": 0.4,
        "notes": "Fixture review. Not an experimental result.",
    })
    return LLMResponse(raw=SynthMessage(content=[block], stop_reason="tool_use"),
                       transcript_id="offline", cost_usd=0, input_tokens=1, output_tokens=1,
                       cache_read=0, cache_write=0)


class CaptureLLM:
    def __init__(self, verdict="neutral"):
        self.verdict = verdict
        self.specs = []

    async def call(self, spec, ctx):
        self.specs.append(spec)
        return _response(self.verdict)


async def session_and_hypothesis(conn, *, citations=None):
    now = datetime.now(UTC)
    session = Session(id="depth", created_at=now, updated_at=now, status="running",
        research_goal="Offline review chain", research_plan=ResearchPlan(objective="Check the chain"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1)
    await sessions.insert(conn, session)
    await hypotheses.insert(conn, Hypothesis(
        id="depth-h", session_id="depth", created_at=now, created_by="generation",
        strategy="literature", title="A claim", summary="A claim about a mechanism",
        full_text="A claim about a mechanism that can be tested.", artifact_path="unused.json",
        citations=citations or [], state="draft"))
    return session


def deps(cfg, conn, llm):
    return AgentDeps(cfg, conn, llm, ToolRegistry(cfg).discover())


def test_strategy_bias_keeps_every_strategy_and_needs_two_samples():
    configured = ["literature", "debate", "assumption", "feedback_driven"]
    assert prefer_effective(configured, {"literature": 1400}) == configured
    ordered = prefer_effective(configured, {"literature": 1100, "debate": 1500})
    assert ordered[0] == "debate"
    assert set(ordered) == set(configured)


@pytest.mark.asyncio
async def test_initial_review_has_no_search_and_continues_when_plausible(tmp_cfg, conn):
    session = await session_and_hypothesis(conn)
    llm = CaptureLLM("neutral")
    result = await ReflectionAgent(deps(tmp_cfg, conn, llm)).execute(Task(
        id="rev-initial", session_id="depth", created_at=datetime.now(UTC),
        agent="reflection", action="ReviewHypothesis", target_id="depth-h",
        payload={"kind": "initial"}))
    assert [tool["name"] for tool in llm.specs[0].tools] == ["record_review"]
    assert result.extra["review_kind"] == "initial"
    assert (await reviews.fetch(conn, result.review_ids[0])).kind == "initial"
    await Supervisor(tmp_cfg)._apply_follow_ups(conn, session, Task(
        id="src", session_id="depth", created_at=datetime.now(UTC), agent="reflection",
        action="ReviewHypothesis"), result)
    async with conn.execute("SELECT payload FROM tasks") as cur:
        payloads = [row[0] for row in await cur.fetchall()]
    assert any('"full"' in payload for payload in payloads)


@pytest.mark.asyncio
async def test_disproved_initial_review_does_not_enter_literature_review(tmp_cfg, conn):
    session = await session_and_hypothesis(conn)
    result = await ReflectionAgent(deps(tmp_cfg, conn, CaptureLLM("disproved"))).execute(Task(
        id="rev-stop", session_id="depth", created_at=datetime.now(UTC),
        agent="reflection", action="ReviewHypothesis", target_id="depth-h",
        payload={"kind": "initial"}))
    await Supervisor(tmp_cfg)._apply_follow_ups(conn, session, Task(
        id="src", session_id="depth", created_at=datetime.now(UTC), agent="reflection",
        action="ReviewHypothesis"), result)
    assert (await hypotheses.fetch(conn, "depth-h")).state == "rejected"
    assert await tasks.count_by_status(conn, "depth") == {}


@pytest.mark.asyncio
async def test_observation_without_an_article_skips_the_model(tmp_cfg, conn):
    await session_and_hypothesis(conn)
    from types import SimpleNamespace
    llm = SimpleNamespace(call=AsyncMock(side_effect=AssertionError("no article means no model call")))
    result = await ReflectionAgent(deps(tmp_cfg, conn, llm)).execute(Task(
        id="rev-obs", session_id="depth", created_at=datetime.now(UTC),
        agent="reflection", action="ReviewHypothesis", target_id="depth-h",
        payload={"kind": "observation"}))
    stored = await reviews.fetch(conn, result.review_ids[0])
    assert stored.kind == "observation" and stored.verdict == "neutral"
    assert "does not confirm novelty" in stored.body
    llm.call.assert_not_called()


@pytest.mark.asyncio
async def test_simulation_review_is_not_ranking_input(tmp_cfg, conn):
    await session_and_hypothesis(conn)
    result = await ReflectionAgent(deps(tmp_cfg, conn, CaptureLLM("neutral"))).execute(Task(
        id="rev-sim", session_id="depth", created_at=datetime.now(UTC),
        agent="reflection", action="ReviewHypothesis", target_id="depth-h",
        payload={"kind": "simulation"}))
    body = await reviews.best_for_reasoning(tmp_cfg, conn, "depth-h")
    assert body is None
    stored = await reviews.fetch(conn, result.review_ids[0])
    assert stored.kind == "simulation"
    assert "不是实验结果" in stored.body


@pytest.mark.asyncio
async def test_passed_simulation_enters_the_tournament(tmp_cfg, conn):
    session = await session_and_hypothesis(conn)
    result = TaskResult(kind="review_completed", hypothesis_ids=["depth-h"],
                        extra={"review_kind": "simulation", "verdict": "neutral"})
    await Supervisor(tmp_cfg)._apply_follow_ups(conn, session, Task(
        id="src", session_id="depth", created_at=datetime.now(UTC), agent="reflection",
        action="ReviewHypothesis"), result)
    async with conn.execute("SELECT action FROM tasks") as cur:
        actions = [row[0] for row in await cur.fetchall()]
    assert actions == ["AddToTournament"]


@pytest.mark.asyncio
async def test_researcher_hypothesis_is_queued_for_initial_review(tmp_cfg, conn):
    now = datetime.now(UTC)
    await sessions.insert(conn, Session(id="human", created_at=now, updated_at=now, status="running",
        research_goal="Offline", research_plan=ResearchPlan(objective="Offline"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1))
    tmp_cfg.embeddings.provider = "hash"
    out = await submit_researcher_hypothesis(
        deps(tmp_cfg, conn, None), "human",
        "A researcher-written claim that a named pore mutation changes dwell time.")
    hyp = await hypotheses.fetch(conn, out["hypothesis_id"])
    assert hyp.created_by == "researcher" and hyp.strategy == "human" and hyp.state == "draft"
    assert out["review_queued"] is True
    async with conn.execute("SELECT payload FROM tasks") as cur:
        payload = (await cur.fetchone())[0]
    assert '"initial"' in payload
