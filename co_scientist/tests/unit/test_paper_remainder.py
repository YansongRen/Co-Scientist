"""Bounded paper behaviors that stay outside the review chain."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.evolution import EvolutionAgent
from co_scientist.agents.ranking import RankingAgent
from co_scientist.llm.types import LLMResponse, SynthMessage, TextBlock, ToolUseBlock
from co_scientist.models import Hypothesis, ResearchPlan, Session, Task
from co_scientist.orchestrator.contacts import suggest_contacts
from co_scientist.orchestrator.strategy_effect import sample_choice, weighted_order
from co_scientist.storage.repos import hypotheses, sessions
from co_scientist.tools.registry import ToolRegistry

_CHILD = {
    "title": "Grounded child",
    "statement": "A retrieved gap becomes a separate testable claim",
    "mechanism": "The parent omitted one measured variable",
    "entities": [],
    "citations": [],
    "novelty_argument": "Unverified",
    "anticipated_outcomes": "Unknown",
}


def test_weighted_order_is_stable_and_biased() -> None:
    configured = ["literature", "debate"]
    weights = {"literature": 1.0, "debate": 9.0}
    assert weighted_order(configured, {"literature": 1.0}, "seed") == configured
    first = weighted_order(configured, weights, "same-seed")
    assert weighted_order(configured, weights, "same-seed") == first
    wins = sum(
        weighted_order(configured, weights, f"seed-{i}")[0] == "debate"
        for i in range(300)
    )
    assert wins > 210
    assert sample_choice(["evolution", "generation"], {"generation": 4.0}, "only-one") == "evolution"


def test_contacts_come_only_from_excerpts() -> None:
    empty = suggest_contacts([{
        "title": "Smith et al. invented this",
        "url": "https://example.test",
        "excerpt": "",
    }])
    assert "不推荐联系人" in empty
    assert "Smith" not in empty
    found = suggest_contacts([{
        "title": "Pore review",
        "url": "https://example.test/pore",
        "excerpt": "Smith et al. measured a longer dwell time. 张三等人重复了该观察。",
    }])
    assert "Smith et al." in found
    assert "张三等人" in found
    assert "https://example.test/pore" in found
    assert "没有发信" in found


class TextLLM:
    def __init__(self):
        self.calls = []

    async def call(self, spec, ctx):
        self.calls.append((spec, ctx))
        if any(tool.get("name") == "record_hypothesis" for tool in (spec.tools or [])):
            content = [ToolUseBlock(name="record_hypothesis", input=_CHILD)]
            stop = "tool_use"
        else:
            content = [TextBlock("A simulated objection.\nbetter idea: 1")]
            stop = "end_turn"
        return LLMResponse(
            raw=SynthMessage(content=content, stop_reason=stop), transcript_id="offline",
            cost_usd=0, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
        )


@pytest.mark.asyncio
async def test_debate_is_multi_turn_and_pairwise_is_single(tmp_cfg, conn):
    now = datetime.now(UTC)
    session = Session(
        id="turns", created_at=now, updated_at=now, status="running",
        research_goal="Offline", research_plan=ResearchPlan(objective="Compare two claims"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1,
    )
    await sessions.insert(conn, session)
    for name, elo in (("a-hyp", 1210), ("b-hyp", 1190)):
        await hypotheses.insert(conn, Hypothesis(
            id=name, session_id="turns", created_at=now, created_by="generation", strategy="literature",
            title=name, summary=name, full_text=name, artifact_path="unused.json",
            state="in_tournament", elo=elo, matches_played=0,
        ))
    pool = await hypotheses.list_for_session(conn, "turns")
    llm = TextLLM()
    agent = RankingAgent(AgentDeps(tmp_cfg, conn, llm, ToolRegistry(tmp_cfg).discover()))
    winner, text, _ = await agent._run_debate(session, pool[0], pool[1], mode="debate")
    assert winner in {"a", "b"}
    assert len(llm.calls) == tmp_cfg.ranking.debate_turns
    assert "Earlier debate turns" in llm.calls[-1][0].user_blocks[0].text
    assert "better idea" in text
    before = len(llm.calls)
    await agent._run_debate(session, pool[0], pool[1], mode="pairwise")
    assert len(llm.calls) == before + 1


@pytest.mark.asyncio
async def test_grounding_registers_a_new_child(tmp_cfg, conn):
    now = datetime.now(UTC)
    session = Session(
        id="ground", created_at=now, updated_at=now, status="running",
        research_goal="Offline", research_plan=ResearchPlan(objective="Fill a gap"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1,
    )
    await sessions.insert(conn, session)
    tmp_cfg.embeddings.provider = "hash"
    for index in range(2):
        await hypotheses.insert(conn, Hypothesis(
            id=f"ground-h{index}", session_id="ground", created_at=now, created_by="generation",
            strategy="literature", title=f"Parent {index}", summary=f"Claim {index}",
            full_text=f"Claim {index} needs a source", artifact_path="unused.json",
            state="in_tournament", elo=1300 - index, matches_played=2,
        ))
    llm = TextLLM()
    result = await EvolutionAgent(AgentDeps(tmp_cfg, conn, llm, ToolRegistry(tmp_cfg).discover())).execute(Task(
        id="evo-ground", session_id="ground", created_at=now, agent="evolution",
        action="EvolveTopHypotheses", payload={"strategies": ["grounding"], "top_k": 2},
    ))
    assert result.hypothesis_ids
    child = await hypotheses.fetch(conn, result.hypothesis_ids[0])
    assert child.strategy == "grounding"
    assert child.created_by == "evolution"
    assert child.parent_ids
    prompt = llm.calls[-1][0].user_blocks[0].text
    assert "search" in prompt.lower()
    assert "record_hypothesis" in prompt
