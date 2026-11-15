"""Offline regressions for strategy allocation, atomic admission and small pools."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.evolution import EvolutionAgent
from co_scientist.agents.generation import GenerationAgent
from co_scientist.agents.supervisor import Supervisor
from co_scientist.models import Hypothesis, ResearchPlan, Session, Task
from co_scientist.storage import db
from co_scientist.storage.repos import hypotheses, sessions, tasks
from co_scientist.tools.registry import ToolRegistry


async def seed(conn, sid="diversity", *, count=0, matches=0):
    now = datetime.now(UTC)
    s = Session(id=sid, created_at=now, updated_at=now, status="running",
        research_goal="Offline mechanism study", research_plan=ResearchPlan(objective="Compare mechanisms"),
        config_snapshot={"web_launch": {"n_initial": 3}}, budget_tokens=100000, budget_usd=1)
    await sessions.insert(conn, s)
    for i in range(count):
        await hypotheses.insert(conn, Hypothesis(id=f"{sid}-h{i}", session_id=sid, created_at=now,
            created_by="generation", strategy="literature", title=f"Mechanism {i}", summary=f"Claim {i}",
            full_text=f"Claim {i}", artifact_path="unused.json", state="in_tournament",
            elo=1200+i*15, matches_played=matches))
    return s


def deps(cfg, conn, llm=None):
    return AgentDeps(cfg, conn, llm or SimpleNamespace(), ToolRegistry(cfg).discover())


class SameEmbedder:
    model = "test-semantic"
    dim = 8

    async def embed(self, texts):
        await asyncio.sleep(0.01)
        a = np.zeros((len(texts), self.dim), dtype="float32")
        a[:, 0] = 1
        return a


@pytest.mark.asyncio
async def test_simultaneous_generation_and_evolution_share_admission(tmp_cfg, conn, monkeypatch):
    await seed(conn)
    tmp_cfg.embeddings.dim = 8
    monkeypatch.setattr("co_scientist.agents.generation.make_embedder", lambda cfg: SameEmbedder())
    monkeypatch.setattr("co_scientist.agents.evolution.make_embedder", lambda cfg: SameEmbedder())
    writers = [await db.connect(tmp_cfg) for _ in range(6)]
    try:
        agents = [GenerationAgent(deps(tmp_cfg, c)) if i % 2 == 0 else EvolutionAgent(deps(tmp_cfg, c))
                  for i, c in enumerate(writers)]
        results = await asyncio.gather(*(a._persist("diversity", {"title": f"T{i}",
            "statement": f"Near paraphrase {i}", "mechanism": "Identical causal mechanism"},
            strategy="literature" if i % 2 == 0 else "simplify") for i, a in enumerate(agents)))
        assert sum(new for _, new in results) == 1
        assert len(await hypotheses.list_for_session(conn, "diversity")) == 1
    finally:
        for c in writers:
            await c.close()


@pytest.mark.asyncio
async def test_initial_strategy_rotation_is_idempotent(tmp_cfg, conn):
    session = await seed(conn)
    sup = Supervisor(tmp_cfg)
    assert await sup._enqueue_initial_generation(conn, session, 3) == 3
    assert await sup._enqueue_initial_generation(conn, session, 3) == 0
    async with conn.execute("SELECT payload FROM tasks ORDER BY rowid") as cur:
        payloads = [json.loads(r[0]) for r in await cur.fetchall()]
    assert [p["strategy"] for p in payloads] == ["literature", "debate", "assumption"]
    assert all(p["n"] == 1 for p in payloads)


@pytest.mark.asyncio
async def test_standard_small_pool_can_evolve(tmp_cfg, conn):
    session = await seed(conn, count=5, matches=2)
    assert await Supervisor(tmp_cfg)._decide_next_steps(conn, session) > 0
    async with conn.execute("SELECT agent FROM tasks") as cur:
        assert "evolution" in [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_evolution_obeys_max_ideas_in_standard_mode(tmp_cfg, conn):
    await seed(conn, count=2)
    tmp_cfg.run.max_ideas = 3
    agent = EvolutionAgent(deps(tmp_cfg, conn))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent, "_dedup_query", AsyncMock(return_value=(None, None)))
    try:
        for i in range(3):
            await agent._persist("diversity", {"statement": f"New child {i}", "parent_ids": ["diversity-h0"]}, strategy="simplify")
        assert len(await hypotheses.list_for_session(conn, "diversity")) == 3
    finally:
        monkeypatch.undo()


def task(sid="diversity", **payload):
    return Task(id="test-generation", session_id=sid, created_at=datetime.now(UTC),
                agent="generation", action="CreateInitialHypotheses", payload=payload)


class RecordingLLM:
    runs_own_loop = False

    def __init__(self, *, multiple=False):
        self.specs = []
        self.multiple = multiple

    async def call(self, spec, ctx):
        from co_scientist.llm.types import LLMResponse, SynthMessage, ToolUseBlock
        self.specs.append((spec, ctx))
        record = {"title": "Alternative mechanism", "statement": "An alternative pathway predicts a distinct perturbation response",
            "mechanism": "A causal candidate requiring experimental testing", "entities": [], "citations": [],
            "novelty_argument": "Unverified", "anticipated_outcomes": "Unknown"}
        blocks = [ToolUseBlock(name="record_hypothesis", input=record)]
        if self.multiple:
            blocks.append(ToolUseBlock(name="record_hypothesis", input=record | {"statement": "A second claim"}))
        return LLMResponse(raw=SynthMessage(content=blocks, stop_reason="tool_use"), transcript_id="offline-only",
            cost_usd=0, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["literature", "debate", "assumption", "feedback_driven"])
async def test_four_real_strategy_paths_each_register_one(tmp_cfg, conn, strategy):
    from co_scientist.models import SystemFeedback
    from co_scientist.storage.repos import feedback
    await seed(conn)
    tmp_cfg.embeddings.provider = "hash"
    if strategy == "feedback_driven":
        await feedback.insert(conn, SystemFeedback(id="fb-existing", session_id="diversity",
            created_at=datetime.now(UTC), source="meta_review", kind="system_feedback", active=True,
            text="Existing candidates ignore measurement noise; test the alternative pathway"))
    llm = RecordingLLM()
    result = await GenerationAgent(deps(tmp_cfg, conn, llm)).execute(task(strategy=strategy, n=1))
    assert len(result.hypothesis_ids) == 1
    pool = await hypotheses.list_for_session(conn, "diversity")
    assert len(pool) == 1 and pool[0].strategy == strategy
    final_spec, final_ctx = llm.specs[-1]
    assert final_ctx.mode == strategy
    prompt = final_spec.user_blocks[0].text
    assert "Batch diversity" in prompt and "Renaming terms" in prompt
    assert "Propose three" not in prompt
    assert result.extra["requested_strategy"] == result.extra["effective_strategy"] == strategy
    if strategy == "debate":
        assert len(llm.specs) == tmp_cfg.generation.debate_turns
        assert result.extra["simulated_turns"] == tmp_cfg.generation.debate_turns - 1
        assert "Proposer" in llm.specs[0][0].user_blocks[0].text
        assert "Completed simulated turns" in prompt
    elif strategy == "assumption":
        assert len(llm.specs) == tmp_cfg.generation.assumption_hops
        assert "Completed simulated turns" in prompt
    else:
        assert len(llm.specs) == 1
        assert result.extra["simulated_turns"] == 0


@pytest.mark.asyncio
async def test_feedback_missing_is_explicit_assumption_fallback(tmp_cfg, conn):
    await seed(conn)
    tmp_cfg.embeddings.provider = "hash"
    result = await GenerationAgent(deps(tmp_cfg, conn, RecordingLLM())).execute(task(strategy="feedback_driven"))
    assert result.extra["requested_strategy"] == "feedback_driven"
    assert result.extra["effective_strategy"] == "assumption"
    assert "No prior feedback" in result.extra["fallback_reason"]
    async with conn.execute("SELECT event FROM events") as cur:
        assert "generation_strategy_fallback" in [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"strategy": "invented"}, {"n": 2}, {"strategy": "not_implemented"}])
async def test_invalid_strategy_or_multi_output_request_is_not_silently_literature(tmp_cfg, conn, payload):
    await seed(conn)
    llm = RecordingLLM()
    with pytest.raises(ValueError):
        await GenerationAgent(deps(tmp_cfg, conn, llm)).execute(task(**payload))
    assert llm.specs == []
    assert await hypotheses.list_for_session(conn, "diversity") == []


@pytest.mark.asyncio
async def test_multiple_terminal_records_are_rejected_without_publishing(tmp_cfg, conn):
    await seed(conn)
    with pytest.raises(ValueError, match="only one"):
        await GenerationAgent(deps(tmp_cfg, conn, RecordingLLM(multiple=True))).execute(task())
    assert await hypotheses.list_for_session(conn, "diversity") == []


def test_strategy_config_rejects_unknown_and_supports_integer_weights():
    from pydantic import ValidationError

    from co_scientist.agents.generation_strategies import allocate
    from co_scientist.config import Config
    with pytest.raises(ValidationError):
        Config(generation={"strategies": ["fake"]})
    cfg = Config(generation={"strategies": ["assumption", "literature", "literature"]})
    assert allocate(cfg, 4) == ["assumption", "literature", "literature", "assumption"]


@pytest.mark.asyncio
async def test_pool_context_is_bounded_and_does_not_cross_sessions(tmp_cfg, conn):
    from co_scientist.agents.generation_strategies import pool_context
    await seed(conn, count=25)
    await seed(conn, "foreign", count=1)
    await conn.execute("UPDATE hypotheses SET dedup_cluster='c0007',summary=? WHERE session_id='diversity'", ("causal pathway "*200,))
    await conn.commit()
    tmp_cfg.generation.context_hypotheses = 3
    tmp_cfg.generation.context_chars = 550
    text = await pool_context(deps(tmp_cfg, conn), "diversity")
    assert "cluster=c0007" in text and "strategy=literature" in text
    assert "foreign-h0" not in text and len(text) < 900
    assert "Claim 24" not in text  # full text was not hydrated


@pytest.mark.asyncio
async def test_embedding_service_failure_has_durable_degraded_mode_and_still_dedups(tmp_cfg, conn, monkeypatch):
    from structlog.testing import capture_logs

    from co_scientist.obs.diversity import collect
    await seed(conn)
    broken = SameEmbedder()
    broken.embed = AsyncMock(side_effect=RuntimeError("test service unavailable"))
    monkeypatch.setattr("co_scientist.agents.generation.make_embedder", lambda cfg: broken)
    agent = GenerationAgent(deps(tmp_cfg, conn))
    with capture_logs() as logs:
        first = await agent._persist("diversity", {"title": "T", "statement": "identical test mechanism"}, strategy="literature")
        second = await agent._persist("diversity", {"title": "T", "statement": "identical test mechanism"}, strategy="debate")
    assert first[1] and not second[1] and first[0] == second[0]
    assert any(r["event"] == "embedding_service_failed_degraded_mode" for r in logs)
    summary = await collect(tmp_cfg, conn, "diversity")
    assert summary["used_hash_fallback"] and summary["dedup_blocked_unique_candidates"] == 1
    assert summary["active_embedding_backend"] == "hash-fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0])
async def test_invalid_vectors_never_mean_no_duplicate(tmp_cfg, conn, monkeypatch, value):
    await seed(conn)
    bad = SameEmbedder()
    bad.embed = AsyncMock(return_value=np.full((1, 8), value, dtype="float32"))
    monkeypatch.setattr("co_scientist.agents.generation.make_embedder", lambda cfg: bad)
    agent = GenerationAgent(deps(tmp_cfg, conn))
    await agent._persist("diversity", {"title": "T", "statement": "the same core claim"}, strategy="literature")
    _, inserted = await agent._persist("diversity", {"title": "T", "statement": "the same core claim"}, strategy="debate")
    assert not inserted


@pytest.mark.asyncio
async def test_failed_vector_commit_is_repaired_before_next_admission(tmp_cfg, conn, monkeypatch):
    from co_scientist.storage.repos import embeddings
    from co_scientist.vectors.store import FaissStore
    await seed(conn)
    tmp_cfg.embeddings.dim = 8
    monkeypatch.setattr("co_scientist.agents.generation.make_embedder", lambda cfg: SameEmbedder())
    agent = GenerationAgent(deps(tmp_cfg, conn))
    monkeypatch.setattr(agent, "_dedup_commit", AsyncMock(side_effect=OSError("simulated interrupted cache write")))
    first, inserted = await agent._persist("diversity", {"statement": "First claim"}, strategy="literature")
    assert inserted
    _, inserted = await agent._persist("diversity", {"statement": "Paraphrased first claim"}, strategy="literature")
    assert not inserted
    store = FaissStore(tmp_cfg, "diversity", 8, model="test-semantic")
    await store.load_or_create()
    assert store.n == 1 and store.hypothesis_at(0) == first
    rows = await embeddings.list_for_session(conn, "diversity")
    assert rows[0]["faiss_offset"] == store.offset_of(first)


@pytest.mark.asyncio
async def test_corrupt_cache_is_preserved_and_rebuilt_from_database(tmp_cfg, conn, monkeypatch):
    from co_scientist.vectors.store import FaissStore
    await seed(conn)
    tmp_cfg.embeddings.dim = 8
    monkeypatch.setattr("co_scientist.agents.generation.make_embedder", lambda cfg: SameEmbedder())
    agent = GenerationAgent(deps(tmp_cfg, conn))
    hid, _ = await agent._persist("diversity", {"statement": "Original claim"}, strategy="literature")
    store = FaissStore(tmp_cfg, "diversity", 8, model="test-semantic")
    store._meta_path.write_text('{"ordered_ids": []}')
    _, inserted = await agent._persist("diversity", {"statement": "Another paraphrase"}, strategy="literature")
    assert not inserted and await hypotheses.fetch(conn, hid)
    assert list(store._dir.glob("*.invalid-*"))


@pytest.mark.asyncio
async def test_equal_dimensions_different_models_cannot_mix_vectors(tmp_cfg):
    from co_scientist.vectors.store import FaissStore
    a, b = [FaissStore(tmp_cfg, "models", 8, model=m) for m in ("semantic", "hash-fallback")]
    await a.load_or_create()
    await a.add("first", np.array([1,0,0,0,0,0,0,0], dtype="float32"))
    await b.load_or_create()
    assert b.n == 0 and a._index_path != b._index_path


@pytest.mark.asyncio
async def test_atomic_capacity_cannot_be_exceeded_by_mixed_writers(tmp_cfg, conn, monkeypatch):
    await seed(conn)
    tmp_cfg.embeddings.provider = "hash"
    tmp_cfg.run.max_ideas = 2
    tmp_cfg.vectors.dedup_cosine_threshold = 1.01
    writers = [await db.connect(tmp_cfg) for _ in range(8)]
    try:
        results = await asyncio.gather(*(GenerationAgent(deps(tmp_cfg, c))._persist("diversity",
            {"statement": f"Independent test claim {i}"}, strategy="literature") if i%2 == 0
            else EvolutionAgent(deps(tmp_cfg, c))._persist("diversity", {"statement": f"Child {i}"}, strategy="simplify")
            for i,c in enumerate(writers)))
        assert sum(new for _,new in results) == 2
        assert len(await hypotheses.list_for_session(conn, "diversity")) == 2
    finally:
        for c in writers:
            await c.close()


@pytest.mark.asyncio
async def test_same_vectors_in_different_sessions_do_not_block_each_other(tmp_cfg, conn, monkeypatch):
    await seed(conn)
    await seed(conn, "other")
    monkeypatch.setattr("co_scientist.agents.generation.make_embedder", lambda cfg: SameEmbedder())
    agent = GenerationAgent(deps(tmp_cfg, conn))
    for sid in ("diversity", "other"):
        _, inserted = await agent._persist(sid, {"statement": "Identical within another session"}, strategy="literature")
        assert inserted


@pytest.mark.asyncio
async def test_proximity_labels_are_consumed_by_ranking_without_deleting_rows(tmp_cfg, conn, monkeypatch):
    from co_scientist.agents.proximity import ProximityAgent
    from co_scientist.agents.ranking import RankingAgent
    await seed(conn, count=3)
    fake = SameEmbedder()
    async def embed(texts):
        return np.array([[1,0,0,0,0,0,0,0] if "Mechanism 2" not in t else [0,1,0,0,0,0,0,0] for t in texts], dtype="float32").reshape((-1,8))
    fake.embed = embed
    monkeypatch.setattr("co_scientist.agents.proximity.make_embedder", lambda cfg: fake)
    result = await ProximityAgent(deps(tmp_cfg, conn)).execute(Task(id="cluster", session_id="diversity",
        created_at=datetime.now(UTC), agent="proximity", action="UpdateProximityGraph", payload={"rebuild": True}))
    pool = sorted(await hypotheses.list_for_session(conn, "diversity"), key=lambda h:h.id)
    assert len(pool) == 3 and all(h.state == "in_tournament" for h in pool)
    assert pool[0].dedup_cluster == pool[1].dedup_cluster != pool[2].dedup_cluster
    assert result.extra["behavior"] == "labels_only_no_deletion"
    ranker = RankingAgent(deps(tmp_cfg, conn))
    # h1 shares a cluster with the target; h2 is a different direction.
    assert ranker._nearest_elo(pool[0], pool[1:]).id == pool[1].id
    tmp_cfg.run.research_mode = "lightweight"
    a,b,_ = await ranker._select_pair("diversity", pool, focus_id=None)
    assert a.dedup_cluster == b.dedup_cluster


@pytest.mark.asyncio
async def test_mark_duplicates_false_keeps_rows_but_records_similarity(tmp_cfg, conn, monkeypatch):
    from co_scientist.obs.diversity import collect
    await seed(conn)
    tmp_cfg.vectors.mark_duplicates = False
    monkeypatch.setattr("co_scientist.agents.generation.make_embedder", lambda cfg: SameEmbedder())
    agent = GenerationAgent(deps(tmp_cfg, conn))
    for i in range(2):
        assert (await agent._persist("diversity", {"statement": f"Similar mechanism {i}"}, strategy="literature"))[1]
    summary = await collect(tmp_cfg, conn, "diversity")
    assert summary["similarity_flagged_candidates"] == 1
    assert summary["high_similarity_ratio"] == 1.0


@pytest.mark.asyncio
async def test_idle_generation_is_bounded_and_restart_idempotent(tmp_cfg, conn):
    s = await seed(conn)
    await Supervisor(tmp_cfg)._enqueue_initial_generation(conn, s, 3)
    async with conn.execute("SELECT id FROM tasks") as cur:
        for row in await cur.fetchall():
            await tasks.complete(conn, row[0])
    for expected_strategy in ("feedback_driven", "literature"):
        assert await Supervisor(tmp_cfg)._schedule_idle_generation(conn, s) == 1
        assert await Supervisor(tmp_cfg)._schedule_idle_generation(conn, s) == 0
        async with conn.execute("SELECT id,payload FROM tasks WHERE status='pending'") as cur:
            row = await cur.fetchone()
        assert json.loads(row["payload"])["strategy"] == expected_strategy
        await tasks.complete(conn, row["id"])
    assert await Supervisor(tmp_cfg)._schedule_idle_generation(conn, s) == 0


@pytest.mark.asyncio
async def test_exhausted_budget_cannot_spawn_evolution_or_generation(tmp_cfg, conn):
    s = await seed(conn, count=5, matches=4)
    await conn.execute("UPDATE sessions SET budget_used_usd=budget_usd WHERE id=?", (s.id,))
    await conn.commit()
    assert await Supervisor(tmp_cfg)._decide_next_steps(conn, s) == 0
    assert await tasks.count_by_status(conn, s.id) == {}


def test_large_mode_maturity_is_preserved_and_small_gate_is_configurable():
    from co_scientist.config import Config
    from co_scientist.orchestrator.research_policy import maturity_decision
    cfg = Config()
    pool = [SimpleNamespace(matches_played=2, elo=1250-i*20) for i in range(5)]
    session = SimpleNamespace(budget_usd=1, budget_used_usd=0, budget_tokens=10000, budget_used_tokens=0)
    assert maturity_decision(cfg, pool, session=session, n_initial=10)["ready"]
    cfg.evolution.small_pool_matches = 3
    assert not maturity_decision(cfg, pool, session=session, n_initial=10)["ready"]
    cfg.evolution.small_pool_matches = 2
    cfg.evolution.small_pool_require_top_separation = True
    cfg.evolution.small_pool_elo_gap = 100
    assert not maturity_decision(cfg, pool, session=session, n_initial=10)["ready"]
    session.budget_usd = 25
    large = maturity_decision(cfg, pool, session=session, n_initial=30)
    assert not large["small_pool"] and large["required_mature"] == 20 and not large["ready"]


@pytest.mark.asyncio
async def test_evolution_child_reenters_reflection_ranking_and_diagnostics(tmp_cfg, conn, monkeypatch):
    from co_scientist.agents.metareview import MetaReviewAgent
    from co_scientist.obs import diversity
    from co_scientist.orchestrator.termination import StabilityTracker
    from co_scientist.storage.artifacts import read_json
    from co_scientist.tests.unit.test_continuous_integration import ScriptedLLM
    tmp_cfg.run.research_mode = "lightweight"
    tmp_cfg.run.max_ideas = 4
    tmp_cfg.run.concurrency = 3
    tmp_cfg.embeddings.provider = "hash"
    # This integration fixture tests scheduling/lineage, not semantic novelty.
    # Separate concurrency regressions above keep real dedup thresholds enabled.
    tmp_cfg.vectors.dedup_cosine_threshold = 1.01
    tmp_cfg.ranking.lightweight_max_pair_matches = 1
    tmp_cfg.evolution.strategies = ["simplify"]
    s = await seed(conn)
    llm = ScriptedLLM(None, None)
    agent_deps = deps(tmp_cfg, conn, llm)
    runner, policy = Supervisor(tmp_cfg), Supervisor(tmp_cfg)
    monkeypatch.setattr(runner, "_decide_next_steps", AsyncMock(return_value=0))
    assert await runner._enqueue_initial_generation(conn, s, 3) == 3
    for phase in range(3):
        await asyncio.wait_for(runner._main_loop(conn, agent_deps, s, StabilityTracker(5,3,25)), timeout=8)
        if phase < 2:
            assert await policy._decide_next_steps(conn, s) == 1
    pool = await hypotheses.list_for_session(conn, s.id)
    assert len(pool) == 4
    assert {h.strategy for h in pool if h.created_by == "generation"} == {"literature", "debate", "assumption"}
    child = next(h for h in pool if h.created_by == "evolution")
    assert child.parent_ids and set(child.parent_ids) <= {h.id for h in pool if h.created_by == "generation"}
    assert child.state == "in_tournament" and child.matches_played > 0
    async with conn.execute("SELECT action,status FROM tasks WHERE target_id=?", (child.id,)) as cur:
        actions = {(r[0], r[1]) for r in await cur.fetchall()}
    assert ("ReviewHypothesis", "done") in actions and ("AddToTournament", "done") in actions
    final = await MetaReviewAgent(agent_deps).execute(Task(id="final", session_id=s.id,
        created_at=datetime.now(UTC), agent="metareview", action="GenerateFinalResearchOverview"))
    report = (tmp_cfg.data_dir / final.extra["overview_path"]).read_text()
    assert "hash embedding fallback" in report and "不能可靠识别语义改写" in report
    stored = await sessions.fetch(conn, s.id)
    summary = stored.config_snapshot["diversity_diagnostics"]
    assert summary["initial_generation_tasks"] == 3
    assert summary["evolution_trigger_count"] == 1
    assert summary["evolution_tasks"][0]["strategies_used"] == ["simplify"]
    assert (await read_json(tmp_cfg, summary["artifact_path"]))["used_hash_fallback"]
    assert (await diversity.collect(tmp_cfg, conn, s.id))["similarity_coverage"]["complete"]


@pytest.mark.asyncio
async def test_optional_top_stability_needs_distinct_match_observations(tmp_cfg, conn):
    s = await seed(conn, count=3, matches=3)
    tmp_cfg.evolution.small_pool_require_top_stability = True
    sup = Supervisor(tmp_cfg)
    assert not (await sup._maturity(conn, s, await hypotheses.list_for_session(conn, s.id)))["ready"]
    for _ in range(4):
        gate = await sup._maturity(conn, s, await hypotheses.list_for_session(conn, s.id))
        assert not gate["top_stable"]  # repeated idle reads are not new ranking evidence
    async with conn.execute("SELECT COUNT(*) FROM events WHERE event='evolution_maturity_observed'") as cur:
        assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_similarity_ratio_is_unknown_when_index_coverage_is_absent(tmp_cfg, conn):
    from co_scientist.obs.diversity import collect
    await seed(conn, count=3)
    summary = await collect(tmp_cfg, conn, "diversity")
    assert summary["high_similarity_ratio"] is None
    assert not summary["similarity_coverage"]["complete"]


@pytest.mark.asyncio
async def test_initial_generation_does_not_exceed_pool_capacity(tmp_cfg, conn):
    s = await seed(conn)
    tmp_cfg.run.max_ideas = 2
    assert await Supervisor(tmp_cfg)._enqueue_initial_generation(conn, s, 10) == 2
    async with conn.execute("SELECT COUNT(*) FROM tasks") as cur:
        assert (await cur.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_two_processes_share_atomic_query_and_commit(tmp_cfg, conn):
    import os
    import subprocess
    import sys
    from pathlib import Path
    await seed(conn)
    # Start independent Python processes with independent SQLite/FAISS objects.
    script = '''
import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
from co_scientist.config import Config,StorageCfg
from co_scientist.storage.db import connect
from co_scientist.agents.base import AgentDeps
from co_scientist.agents.generation import GenerationAgent
from co_scientist.tools.registry import ToolRegistry
async def main():
    cfg=Config(storage=StorageCfg(data_dir=sys.argv[1]))
    cfg.embeddings.provider="hash"
    cfg.embeddings.dim=32
    conn=await connect(cfg)
    try:
        Path(sys.argv[1],sys.argv[2]+".ready").touch()
        for _ in range(300):
            if len(list(Path(sys.argv[1]).glob("*.ready"))) == 2: break
            await asyncio.sleep(.01)
        result=await GenerationAgent(AgentDeps(cfg,conn,SimpleNamespace(),ToolRegistry(cfg).discover()))._persist(
            "diversity",{"title":"T","statement":"The same core mechanism in two processes"},strategy=sys.argv[2])
        print("ADMISSION_RESULT:"+json.dumps(result),flush=True)
    finally:
        await conn.close()
asyncio.run(main())
'''
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_cfg.data_dir), strategy],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        cwd=Path(__file__).resolve().parents[3]) for strategy in ("literature", "debate")]
    try:
        outputs = await asyncio.gather(*(asyncio.to_thread(p.communicate, timeout=12) for p in processes))
        results = []
        for p,(stdout,stderr) in zip(processes,outputs,strict=True):
            assert p.returncode == 0, stderr
            results.append(json.loads(next(line.split(":",1)[1] for line in stdout.splitlines()
                                           if line.startswith("ADMISSION_RESULT:"))))
        assert sum(r[1] for r in results) == 1
        assert results[0][0] == results[1][0]
        assert len(await hypotheses.list_for_session(conn, "diversity")) == 1
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=3)


@pytest.mark.asyncio
async def test_faiss_reader_cannot_observe_half_published_index_pair(tmp_cfg, monkeypatch):
    import threading
    import time

    from co_scientist.vectors import store as store_module
    from co_scientist.vectors.store import FaissStore
    writer = FaissStore(tmp_cfg, "paired-cache", 8)
    await writer.load_or_create()
    await writer.add("first", np.eye(8, dtype="float32")[0])
    replaced = threading.Event()
    original = store_module.os.replace
    def slow_replace(src, dst):
        original(src, dst)
        if str(dst).endswith("index.faiss"):
            replaced.set()
            time.sleep(0.05)  # intentionally expose the two-file publication gap
    monkeypatch.setattr(store_module.os, "replace", slow_replace)
    async def read_after_index_replace():
        assert await asyncio.to_thread(replaced.wait, 2)
        reader = FaissStore(tmp_cfg, "paired-cache", 8)
        await reader.load_or_create()
        assert reader.n == 2
        assert {reader.hypothesis_at(i) for i in range(reader.n)} == {"first", "second"}
    await asyncio.gather(writer.add("second", np.eye(8,dtype="float32")[1]),
                         *(read_after_index_replace() for _ in range(6)))


@pytest.mark.asyncio
async def test_optional_top_stability_uses_real_match_count_progress(tmp_cfg, conn):
    from co_scientist.models import TournamentMatch
    from co_scientist.storage.repos import tournaments
    s = await seed(conn, count=3, matches=3)
    tmp_cfg.evolution.small_pool_require_top_stability = True
    sup = Supervisor(tmp_cfg)
    for index in range(3):
        await tournaments.insert_match(conn, TournamentMatch(id=f"stable-{index}", session_id=s.id,
            created_at=datetime.now(UTC), hyp_a="diversity-h0", hyp_b="diversity-h1", mode="pairwise",
            winner="a", elo_a_before=1200, elo_b_before=1215))
        gate = await sup._maturity(conn, s, await hypotheses.list_for_session(conn, s.id))
        assert gate["top_stable"] == (index == 2)
    assert gate["ready"]


@pytest.mark.asyncio
async def test_dimension_change_repairs_embedding_metadata_without_removing_hypotheses(tmp_cfg, conn):
    from co_scientist.storage.repos import embeddings
    await seed(conn)
    tmp_cfg.embeddings.provider = "hash"
    tmp_cfg.embeddings.dim = 8
    tmp_cfg.vectors.dedup_cosine_threshold = 1.01
    agent = GenerationAgent(deps(tmp_cfg, conn))
    await agent._persist("diversity", {"statement": "First immutable hypothesis"}, strategy="literature")
    tmp_cfg.embeddings.dim = 16
    await agent._persist("diversity", {"statement": "Second immutable hypothesis"}, strategy="literature")
    metadata = await embeddings.list_for_session(conn, "diversity")
    assert len(metadata) == 2 and all(r["dim"] == 16 for r in metadata)
    assert len(await hypotheses.list_for_session(conn, "diversity")) == 2


@pytest.mark.asyncio
async def test_new_top_candidates_cannot_inherit_old_stability(tmp_cfg, conn):
    from co_scientist.storage.repos import events
    s = await seed(conn, count=3, matches=3)
    tmp_cfg.evolution.small_pool_require_top_stability = True
    for n in range(3):
        await events.emit(conn, session_id=s.id, task_id=None, agent="supervisor",
            event="evolution_maturity_observed", payload={"match_count": n-2,
                "top_ids": ["diversity-h2", "diversity-h1"], "top_elos": [1230,1215], "pool_size":3})
    # Keep the same current match-count anchor, change who actually ranks first.
    await conn.execute("UPDATE hypotheses SET elo=1500 WHERE id='diversity-h0'")
    await conn.commit()
    gate = await Supervisor(tmp_cfg)._maturity(conn, s, await hypotheses.list_for_session(conn, s.id))
    assert not gate["top_stable"] and not gate["ready"]


def test_legacy_zero_budget_and_naive_deadline_keep_existing_termination_semantics():
    from datetime import timedelta

    from co_scientist.config import Config
    from co_scientist.orchestrator.research_policy import maturity_decision
    now = datetime.now(UTC)
    session = Session(id="legacy", created_at=now, updated_at=now, status="running",
        research_goal="Offline", research_plan=ResearchPlan(objective="Offline"), config_snapshot={},
        budget_tokens=0, budget_usd=0, wall_deadline=(now+timedelta(minutes=1)).replace(tzinfo=None))
    cfg = Config()
    assert Supervisor(cfg)._remaining_budget(session)
    pool = [SimpleNamespace(matches_played=3, elo=1200+i*20) for i in range(3)]
    assert maturity_decision(cfg,pool,session=session,n_initial=3)["budget_ok"]
