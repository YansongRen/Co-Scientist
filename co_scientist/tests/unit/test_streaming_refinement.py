"""A slow unrelated hypothesis must not block a ready candidate feedback loop."""

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from co_scientist.agents.evolution import EvolutionAgent
from co_scientist.agents.supervisor import Supervisor
from co_scientist.models import Review, Task
from co_scientist.orchestrator.termination import StabilityTracker
from co_scientist.storage import db
from co_scientist.storage.repos import hypotheses, reviews, sessions, tasks
from co_scientist.tests.unit.test_continuous_integration import ScriptedLLM
from co_scientist.tests.unit.test_hypothesis_diversity import RecordingLLM, deps, seed


async def prepared(cfg, conn, *, matches=2):
    s = await seed(conn, count=2, matches=matches)
    s.config_snapshot["evolution"] = cfg.evolution.model_dump()
    s.config_snapshot["run"] = cfg.run.model_dump()
    await conn.execute(
        "UPDATE sessions SET config_snapshot=? WHERE id=?", (json.dumps(s.config_snapshot), s.id)
    )
    await conn.commit()
    for n in range(2):
        await add_review(conn, n)
    return s


async def add_review(conn, n, version="v1", kind="full"):
    await reviews.insert(
        conn,
        Review(
            id=f"review-{n}-{version}",
            session_id="diversity",
            hypothesis_id=f"diversity-h{n}",
            created_at=datetime.now(UTC),
            kind=kind,
            body=f"Offline review {version}; causal mechanism unverified",
            artifact_path="unused-review.json",
        ),
    )


async def evolution_task(conn):
    async with conn.execute(
        "SELECT id,payload FROM tasks WHERE agent='evolution' ORDER BY rowid DESC LIMIT 1"
    ) as cur:
        r = await cur.fetchone()
    return Task(
        id=r["id"],
        session_id="diversity",
        created_at=datetime.now(UTC),
        agent="evolution",
        action="EvolveTopHypotheses",
        payload=json.loads(r["payload"]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("from_scratch", [False, True])
async def test_evolution_and_child_review_finish_before_blocked_generation(
    tmp_cfg, conn, monkeypatch, from_scratch
):
    tmp_cfg.run.concurrency = 3
    tmp_cfg.run.research_mode = "lightweight"
    tmp_cfg.run.max_ideas = 4
    tmp_cfg.embeddings.provider = "hash"
    tmp_cfg.vectors.dedup_cosine_threshold = 1.01
    tmp_cfg.evolution.strategies = ["simplify"]
    tmp_cfg.ranking.lightweight_max_pair_matches = 1
    if from_scratch:
        s = await seed(conn)
        s.config_snapshot["evolution"] = tmp_cfg.evolution.model_dump()
        s.config_snapshot["run"] = tmp_cfg.run.model_dump()
        await conn.execute(
            "UPDATE sessions SET config_snapshot=? WHERE id=?",
            (json.dumps(s.config_snapshot), s.id),
        )
        await conn.commit()
    else:
        s = await prepared(tmp_cfg, conn, matches=0)
    entered, release, evolved = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingFake(ScriptedLLM):
        async def call(self, spec, ctx):
            if ctx.agent == "generation" and (not from_scratch or ctx.mode == "assumption"):
                entered.set()
                await release.wait()
            elif ctx.agent == "evolution":
                assert entered.is_set() and not release.is_set()
                evolved.set()
            return await super().call(spec, ctx)

    now = datetime.now(UTC)
    sup = Supervisor(tmp_cfg)
    if from_scratch:
        await sup._enqueue_initial_generation(conn, s, 3)
    else:
        await tasks.enqueue(
            conn,
            Task(
                id="slow-generation",
                session_id=s.id,
                created_at=now,
                agent="generation",
                action="CreateInitialHypotheses",
                priority=10,
                payload={"strategy": "literature", "n": 1},
            ),
        )
        await tasks.enqueue(
            conn,
            Task(
                id="local-ranking",
                session_id=s.id,
                created_at=now,
                agent="ranking",
                action="RunTournamentBatch",
                priority=20,
            ),
        )
    # This test deliberately disables idle refill. Any Evolution observed here
    # must come from ready-candidate dispatch while the queue is still busy.
    monkeypatch.setattr(sup, "_decide_next_steps", AsyncMock(return_value=0))
    observer = await db.connect(tmp_cfg)
    job = asyncio.create_task(
        sup._main_loop(
            conn, deps(tmp_cfg, conn, BlockingFake(None, None)), s, StabilityTracker(5, 3, 25)
        )
    )
    try:
        await asyncio.wait_for(evolved.wait(), 3)

        async def child_reviewed():
            while True:
                pool = await hypotheses.list_for_session(observer, s.id)
                children = [
                    h for h in pool if h.created_by == "evolution" and h.state == "in_tournament"
                ]
                if children:
                    return children[0]
                await asyncio.sleep(0.01)

        child = await asyncio.wait_for(child_reviewed(), 3)
        initial_pool = [
            h
            for h in await hypotheses.list_for_session(observer, s.id)
            if h.created_by == "generation"
        ]
        assert len(initial_pool) == 2 and child.parent_ids
        assert set(child.parent_ids) <= {h.id for h in initial_pool}
        assert not release.is_set()
        async with observer.execute(
            "SELECT COUNT(*) FROM tasks WHERE agent='generation' AND status='in_progress'"
        ) as cur:
            assert (await cur.fetchone())[0] == 1
        async with observer.execute("SELECT status FROM tasks WHERE agent='evolution'") as cur:
            assert (await cur.fetchone())[0] == "done"
        release.set()
        await asyncio.wait_for(job, 5)
        assert len(await hypotheses.list_for_session(observer, s.id)) <= 4
    finally:
        release.set()
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await observer.close()


@pytest.mark.asyncio
async def test_ready_dispatch_is_restart_idempotent_and_elo_drift_does_not_create_round(
    tmp_cfg, conn
):
    s = await prepared(tmp_cfg, conn)
    assert await Supervisor(tmp_cfg)._schedule_ready_refinement(conn, s) == 1
    first = await evolution_task(conn)
    assert await Supervisor(tmp_cfg)._schedule_ready_refinement(conn, s) == 0
    await tasks.complete(conn, first.id)
    await conn.execute("UPDATE hypotheses SET elo=elo+5 WHERE session_id=?", (s.id,))
    await conn.commit()
    assert await Supervisor(tmp_cfg)._schedule_ready_refinement(conn, s) == 0
    await add_review(conn, 0, "v2")
    assert await Supervisor(tmp_cfg)._schedule_ready_refinement(conn, s) == 1
    assert (await evolution_task(conn)).payload["candidate_revision"] != first.payload[
        "candidate_revision"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block", ["budget", "capacity", "paused", "unreviewed", "simulation", "round_limit"]
)
async def test_ready_dispatch_respects_resource_and_evidence_guards(tmp_cfg, conn, block):
    s = await prepared(tmp_cfg, conn)
    sup = Supervisor(tmp_cfg)
    if block == "budget":
        await conn.execute("UPDATE sessions SET budget_used_usd=budget_usd WHERE id=?", (s.id,))
    elif block == "capacity":
        tmp_cfg.run.max_ideas = 2
    elif block == "paused":
        await sessions.set_status(conn, s.id, "paused")
    elif block == "unreviewed":
        await conn.execute("DELETE FROM reviews")
    elif block == "simulation":
        await conn.execute("UPDATE reviews SET kind='simulation'")
    elif block == "round_limit":
        tmp_cfg.evolution.max_streaming_rounds = 1
        assert await sup._schedule_ready_refinement(conn, s) == 1
        await tasks.complete(conn, (await evolution_task(conn)).id)
        await add_review(conn, 0, "v2")
    await conn.commit()
    assert await sup._schedule_ready_refinement(conn, s) == 0


@pytest.mark.asyncio
async def test_evolution_uses_frozen_parents_and_reviews_despite_new_top(tmp_cfg, conn):
    s = await prepared(tmp_cfg, conn)
    tmp_cfg.embeddings.provider = "hash"
    tmp_cfg.evolution.strategies = ["simplify"]
    assert await Supervisor(tmp_cfg)._schedule_ready_refinement(conn, s) == 1
    task = await evolution_task(conn)
    h = await hypotheses.fetch(conn, "diversity-h0")
    await hypotheses.insert(conn, h.model_copy(update={"id": "late-top", "elo": 9999}))
    await add_review(conn, 0, "v2")
    llm = RecordingLLM()
    result = await EvolutionAgent(deps(tmp_cfg, conn, llm)).execute(task)
    assert result.extra["parent_ids"] == task.payload["parent_ids"]
    assert "late-top" not in result.extra["parent_ids"]
    child = await hypotheses.fetch(conn, result.hypothesis_ids[0])
    assert set(child.parent_ids) <= set(task.payload["parent_ids"])
    assert await hypotheses.fetch(conn, "late-top")
    assert "Offline review v1" in llm.specs[0][0].user_blocks[0].text


@pytest.mark.asyncio
async def test_foreign_parent_is_rejected_before_model_call(tmp_cfg, conn):
    s = await prepared(tmp_cfg, conn)
    await seed(conn, "foreign", count=1)
    llm = RecordingLLM()
    t = Task(
        id="foreign-parent",
        session_id=s.id,
        created_at=datetime.now(UTC),
        agent="evolution",
        action="EvolveTopHypotheses",
        payload={"parent_ids": ["diversity-h0", "foreign-h0"]},
    )
    with pytest.raises(ValueError, match="session mismatch"):
        await EvolutionAgent(deps(tmp_cfg, conn, llm)).execute(t)
    assert llm.specs == []


@pytest.mark.asyncio
async def test_legacy_sessions_do_not_silently_enable_new_dispatch(tmp_cfg, conn):
    s = await seed(conn, count=2, matches=4)
    assert not Supervisor(tmp_cfg)._streaming_enabled(s)
    assert await Supervisor(tmp_cfg)._schedule_ready_refinement(conn, s) == 0
