"""Run real Agent classes and Supervisor with a scripted, zero-network LLM.

Fixtures assert plumbing and provenance, never biological performance.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.supervisor import Supervisor
from co_scientist.llm.types import LLMResponse, SynthMessage, TextBlock, ToolUseBlock
from co_scientist.models import ResearchPlan, Session, Task
from co_scientist.orchestrator.termination import StabilityTracker
from co_scientist.storage.repos import hypotheses, sessions, tasks
from co_scientist.storage.repos import scientific_evidence as evidence
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
            if self.path and self.generations in {1, 3}:
                record["protein_design_request"] = {"manifest_path": str(self.path), "output_dir": str(self.output), "mode": "mock"}
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
@pytest.mark.parametrize("protein,concurrency", [(False, 1), (True, 1), (True, 3)])
async def test_all_six_agents_and_second_design_round(tmp_cfg, conn, tmp_path, monkeypatch, protein, concurrency):
    # Explicit hash embeddings prevent environment API keys selecting a provider.
    tmp_cfg.embeddings.provider = "hash"
    tmp_cfg.vectors.dedup_cosine_threshold = 1.01
    tmp_cfg.run.research_mode = "lightweight"
    tmp_cfg.run.concurrency = concurrency
    tmp_cfg.run.max_ideas = 3
    tmp_cfg.ranking.lightweight_max_pair_matches = 1
    tmp_cfg.vectors.full_recluster_every_matches = 1
    path = tmp_path / "kemp.json"
    (tmp_path / "motif.cif").write_text("data_OFFLINE_NOT_A_REAL_MOTIF\n")
    path.write_text(json.dumps({"defaults": {"input_file": "motif.cif", "motif_str": "A1", "motif_atoms": "A1:NE2"},
        "designs": [{"name": "kemp", "num_designs": 1}], "evaluation": {"suite": "kemp_eliminase"}}))
    now = datetime.now(UTC)
    sid = "offline-integrated"
    session = Session(id=sid, created_at=now, updated_at=now, status="running", research_goal="Public Kemp fixture" if protein else "Non-protein instrument calibration",
        research_plan=ResearchPlan(objective="Offline mechanism validation"), config_snapshot={}, budget_tokens=100000, budget_usd=1)
    await sessions.insert(conn, session)
    llm = ScriptedLLM(path if protein else None, tmp_path / "ignored-model-path")
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
    designs = [t for t in results if t["agent"] == "scientific_tool"]
    if protein:
        assert len(designs) == 2
        child_task = next(t for t in designs if t["target_id"] == evolved.id)
        payload = json.loads(child_task["payload"])
        assert payload["design_iteration"] == 2
        assert payload["parent_hypothesis_id"] in evolved.parent_ids
        rows = await evidence.list_for_task(conn, child_task["id"])
        assert len(rows) == 11 and {r.status for r in rows} == {"mock"}
        assert {r.design_iteration for r in rows} == {2}
        assert await evidence.render_for_agents(conn, [evolved.id], include_mock=False, include_invalid=False)
        for call in llm.calls:
            if call["agent"] in {"ranking", "evolution"}:
                assert "value=0.84" not in call["prompt"]
    else:
        assert designs == []
    await runner._finalize(conn, deps, session, None)
    assert (await sessions.fetch(conn, sid)).status == "done"
    print(json.dumps({"case": f"protein={protein},concurrency={concurrency}", "tasks": Counter(t["agent"] for t in results),
        "designs": [{"task_id": t["id"], "hypothesis_id": t["target_id"], "iteration": json.loads(t["payload"])["design_iteration"]} for t in designs],
        "llm_calls": len(llm.calls), "cost_usd": 0, "data_dir": str(tmp_cfg.data_dir)}, ensure_ascii=False))
