"""Execution provenance and real scheduler overlap, with a zero-cost Fake LLM."""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.supervisor import Supervisor
from co_scientist.models import Task, TaskResult
from co_scientist.orchestrator.termination import StabilityTracker
from co_scientist.storage.repos import hypotheses
from co_scientist.tests.unit.test_continuous_integration import ScriptedLLM
from co_scientist.tests.unit.test_hypothesis_diversity import seed
from co_scientist.tools.registry import ToolRegistry
from co_scientist.web.research_start import LaunchOptions, config_for_launch
from co_scientist.web.runtime import runtime_snapshot


@pytest.mark.parametrize('mode', ['standard','lightweight'])
def test_explicit_concurrency_survives_both_launch_paths(tmp_cfg, mode):
    cfg = config_for_launch(tmp_cfg, LaunchOptions(concurrency=3,research_mode=mode))
    assert cfg.run.concurrency == 3
    assert tmp_cfg.run.concurrency == 4  # server config is not mutated
    assert config_for_launch(tmp_cfg, LaunchOptions()).run.concurrency == 1
    with pytest.raises(ValidationError):
        LaunchOptions(concurrency=5)


@pytest.mark.asyncio
async def test_handoffs_only_describe_new_durable_tasks_and_are_session_scoped(tmp_cfg, conn):
    s = await seed(conn,count=1)
    other = await seed(conn,'foreign',count=1)
    t = Task(id='source',session_id=s.id,created_at=datetime.now(UTC),agent='generation',action='CreateInitialHypotheses')
    result=TaskResult(kind='hypothesis_created',hypothesis_ids=['diversity-h0'])
    sup=Supervisor(tmp_cfg)
    await sup._apply_follow_ups(conn,s,t,result)
    await sup._apply_follow_ups(conn,s,t,result)
    r=await runtime_snapshot(tmp_cfg,conn,s)
    assert len(r['handoffs']) == 1
    event=r['handoffs'][0]
    assert (event['source_agent'],event['destination_agent']) == ('generation','reflection')
    assert event['source_task_id'] == 'source' and event['hypothesis_id'] == 'diversity-h0'
    assert event['delivery'] == 'queued'  # does not pretend the receiver has executed
    async with conn.execute('SELECT status FROM tasks WHERE id=?',(event['destination_task_id'],)) as cur:
        assert (await cur.fetchone())[0] == 'pending'
    assert not (await runtime_snapshot(tmp_cfg,conn,other))['handoffs']


@pytest.mark.asyncio
@pytest.mark.parametrize('concurrency', [1,3])
async def test_multiple_agent_types_really_overlap_in_existing_worker_pool(tmp_cfg,conn,monkeypatch,concurrency):
    s=await seed(conn)
    tmp_cfg.run.concurrency=concurrency
    tmp_cfg.run.max_ideas=4
    tmp_cfg.embeddings.provider='hash'
    tmp_cfg.vectors.dedup_cosine_threshold=1.01
    class SlowFake(ScriptedLLM):
        async def call(self,spec,ctx):
            # Different completion times allow Reflection to start while other
            # Generation calls are still in flight; no real Provider is used.
            await asyncio.sleep(.04 if ctx.agent=='generation' and ctx.task_id==first else .18)
            return await super().call(spec,ctx)
    llm=SlowFake(None,None)
    sup=Supervisor(tmp_cfg)
    await sup._enqueue_initial_generation(conn,s,3)
    async with conn.execute('SELECT id FROM tasks ORDER BY rowid LIMIT 1') as cur:
        first=(await cur.fetchone())[0]
    monkeypatch.setattr(sup,'_decide_next_steps',AsyncMock(return_value=0))
    job=asyncio.create_task(sup._main_loop(conn,AgentDeps(tmp_cfg,conn,llm,ToolRegistry(tmp_cfg).discover()),s,StabilityTracker(5,3,25)))
    overlap=0
    try:
        while not job.done():
            snap=await runtime_snapshot(tmp_cfg,conn,s)
            overlap=max(overlap,snap['execution']['active_agents'])
            await asyncio.sleep(.015)
        await job
    finally:
        if not job.done():
            job.cancel()
            await asyncio.gather(job,return_exceptions=True)
    assert overlap == 1 if concurrency==1 else overlap>=2
    assert len(await hypotheses.list_for_session(conn,s.id)) == 3
    assert (await runtime_snapshot(tmp_cfg,conn,s))['handoffs']


@pytest.mark.asyncio
async def test_paused_snapshot_has_no_active_execution(tmp_cfg,conn):
    s=await seed(conn)
    s.status='paused'
    r=await runtime_snapshot(tmp_cfg,conn,s)
    assert r['execution']['active_agents'] == 0
