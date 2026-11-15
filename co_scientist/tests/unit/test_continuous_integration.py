"""Run real Agent classes and Supervisor with a scripted, zero-network LLM.

Fixtures assert plumbing and provenance, never biological performance.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.supervisor import Supervisor
from co_scientist.llm.types import LLMResponse, SynthMessage, TextBlock, ToolUseBlock
from co_scientist.models import ResearchPlan, Session, Task
from co_scientist.orchestrator.termination import StabilityTracker
from co_scientist.storage.repos import hypotheses, sessions, tasks
from co_scientist.tools.registry import ToolRegistry


class ScriptedLLM:
    runs_own_loop = False

    def __init__(self, path, output):
        self.path, self.output = path, output
        self.calls = []
        self.generations = 0

    async def call(self, spec, ctx):
        prompt = "\n".join(b.text for b in spec.user_blocks)
        self.calls.append({"agent": ctx.agent, "action": ctx.action, "prompt": prompt})
        if ctx.agent in {"generation", "evolution"}:
            self.generations += 1
            statement = ["Preserve catalytic base with a compact hydrophobic pocket.",
                         "Test solvent exposure using an alternative flexible loop scaffold.",
                         "Inspect multi-state geometric consistency before changing the supplied motif."][min(self.generations - 1, 2)]
            record = {"title": f"Offline candidate {self.generations}", "statement": statement,
                "mechanism": statement, "entities": ["Kemp example"], "anticipated_outcomes": "Unknown until experimental testing.",
                "novelty_argument": "Not evaluated by this software fixture.", "citations": []}
            blocks = [ToolUseBlock(name="record_hypothesis", input=record)]
        elif ctx.agent == "reflection":
            record = {"verdict": "missing_piece", "kind": "full", "evidence": [], "novelty": 0.5,
                "correctness": 0.5, "testability": 0.5, "feasibility": 0.5,
                "notes": "Mock fixture: numerical geometry cannot establish experimental catalysis."}
            blocks = [ToolUseBlock(name="record_review", input=record)]
        elif ctx.action == "GenerateSystemFeedback":
            blocks = [ToolUseBlock(name="record_system_feedback", input={"narrative": "Preserve input provenance; experimental activity is unavailable."})]
        elif ctx.agent == "ranking":
            blocks = [TextBlock("Offline deterministic comparison; not a scientific result.\nbetter idea: 1")]
        else:
            blocks = [TextBlock("# Offline validation report\nSoftware mechanism only. Experimental activity unverified.")]
        return LLMResponse(raw=SynthMessage(content=blocks, stop_reason="tool_use" if isinstance(blocks[0], ToolUseBlock) else "end_turn"),
                           transcript_id=f"offline-{len(self.calls)}", cost_usd=0, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 3])
async def test_all_six_agents_and_second_design_round(tmp_cfg, conn, tmp_path, monkeypatch, concurrency):
    # Explicit hash embeddings prevent environment API keys selecting a provider.
    tmp_cfg.embeddings.provider = "hash"
    tmp_cfg.vectors.dedup_cosine_threshold = 1.01
    tmp_cfg.run.research_mode = "lightweight"
    tmp_cfg.run.concurrency = concurrency
    tmp_cfg.run.max_ideas = 3
    tmp_cfg.ranking.lightweight_max_pair_matches = 1
    tmp_cfg.vectors.full_recluster_every_matches = 1
    now = datetime.now(UTC)
    sid = "offline-integrated"
    session = Session(id=sid, created_at=now, updated_at=now, status="running", research_goal="Non-protein instrument calibration",
        research_plan=ResearchPlan(objective="Offline mechanism validation"), config_snapshot={}, budget_tokens=100000, budget_usd=1)
    await sessions.insert(conn, session)
    llm = ScriptedLLM(None, tmp_path / "ignored-model-path")
    deps = AgentDeps(tmp_cfg, conn, llm, ToolRegistry(tmp_cfg).discover())
    runner, policy = Supervisor(tmp_cfg), Supervisor(tmp_cfg)
    monkeypatch.setattr(runner, "_decide_next_steps", AsyncMock(return_value=0))
    for n in range(2):
        await tasks.enqueue(conn, Task(id=f"generation-{n}", session_id=sid, created_at=now,
            agent="generation", action="CreateInitialHypotheses"))
    for phase in range(3):
        await asyncio.wait_for(runner._main_loop(conn, deps, session, StabilityTracker(5, 3, 25)), timeout=8)
        if phase < 2:
            assert await policy._decide_next_steps(conn, session) == 1
    all_hyps = await hypotheses.list_for_session(conn, sid)
    assert len(all_hyps) == 3
    evolved = next(h for h in all_hyps if h.created_by == "evolution")
    assert evolved.parent_ids
    async with conn.execute("SELECT id,agent,action,status,target_id,payload FROM tasks ORDER BY created_at,id") as cur:
        results = [dict(r) for r in await cur.fetchall()]
    assert all(t["status"] == "done" for t in results), results
    async with conn.execute("SELECT COUNT(*) n FROM tournament_matches WHERE session_id=? GROUP BY MIN(hyp_a,hyp_b),MAX(hyp_a,hyp_b)", (sid,)) as cur:
        assert all(r["n"] <= 1 for r in await cur.fetchall())
    assert {t["agent"] for t in results} >= {"generation", "reflection", "ranking", "evolution", "proximity", "metareview"}
    assert not any(t["agent"] == "scientific_tool" for t in results)
    await runner._finalize(conn, deps, session, None)
    assert (await sessions.fetch(conn, sid)).status == "done"
