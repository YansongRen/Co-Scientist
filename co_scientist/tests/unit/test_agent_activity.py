"""Exercise in-flight monitoring with local fakes; no network/model calls."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from co_scientist.llm.routing import ModelRoute
from co_scientist.llm.tool_loop import _dispatch
from co_scientist.llm.types import AgentCallSpec, CallContext, LLMResponse
from co_scientist.models import ResearchPlan, Session, Task
from co_scientist.obs.activity import activity_task, observe_llm_call, retry_sleep
from co_scientist.storage.repos import events, sessions, tasks
from co_scientist.tools.base import ToolResult
from co_scientist.tools.registry import ToolRegistry
from co_scientist.web.app import create_app


async def seed(conn, *, agent="generation", task_id="task-local", status="in_progress"):
    now = datetime.now(UTC)
    if await sessions.fetch(conn, "session-local") is None:
        await sessions.insert(conn, Session(
            id="session-local", created_at=now, updated_at=now, status="running",
            research_goal="Local monitor test", research_plan=ResearchPlan(objective="Test"),
            config_snapshot={}, budget_tokens=1000, budget_usd=1,
        ))
    await tasks.enqueue(conn, Task(
        id=task_id, session_id="session-local", created_at=now, started_at=now,
        agent=agent, action="CreateInitialHypotheses" if agent == "generation" else "ReviewHypothesis",
        status=status,
    ))


async def snapshot(client):
    response = await client.get("/api/studio/sessions/session-local/runtime")
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_live_model_retry_and_cancellation_are_observable(tmp_cfg, conn):
    await seed(conn)
    entered, release, retry_entered, retry_release = (asyncio.Event() for _ in range(4))

    class FakeProvider:
        _cfg, _db = tmp_cfg, conn

        @observe_llm_call
        async def call(self, spec, ctx):
            assert ctx.task_id == "task-local"
            entered.set()
            await release.wait()

            async def wait(_):
                retry_entered.set()
                await retry_release.wait()

            await retry_sleep(0, 1, TimeoutError(), sleep=wait)
            return LLMResponse(raw=None, transcript_id="local-transcript", cost_usd=0,
                               input_tokens=10, output_tokens=5, cache_read=0, cache_write=0)

    spec = AgentCallSpec(route=ModelRoute(agent="generation", mode="literature", model="fake-local"))
    ctx = CallContext("session-local", None, "generation", "CreateInitialHypotheses")

    def launch():
        with activity_task(SimpleNamespace(session_id="session-local", id="task-local", agent="generation")):
            return asyncio.create_task(FakeProvider().call(spec, ctx))
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        job = launch()
        await asyncio.wait_for(entered.wait(), 2)
        data = await snapshot(client)
        generation = data["agents"][0]
        assert generation["status"] == "running"
        assert generation["operations"][0]["phase"] == "llm_call_started"
        assert generation["operations"][0]["model"] == "fake-local"
        release.set()
        await asyncio.wait_for(retry_entered.wait(), 2)
        assert (await snapshot(client))["agents"][0]["operations"][0]["phase"] == "llm_retry_wait"
        retry_release.set()
        await job
        data = await snapshot(client)
        assert data["agents"][0]["operations"] == []
        assert "llm_call_completed" in [e["event"] for e in data["agents"][0]["events"]]

        # A cancelled model call must not remain "running" after reconnect.
        entered.clear()
        release.clear()
        job = launch()
        await asyncio.wait_for(entered.wait(), 2)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert (await snapshot(client))["agents"][0]["operations"] == []


@pytest.mark.asyncio
async def test_parallel_tools_keep_independent_spans_and_redact_secrets(tmp_cfg, conn):
    await seed(conn)
    entered = {name: asyncio.Event() for name in ("one", "two")}
    release = {name: asyncio.Event() for name in ("one", "two")}
    tmp_cfg.secrets.OPENAI_API_KEY = "private-test-key"

    class FakeTool:
        name = "local_search"

        async def call(self, args, ctx):
            entered[args["which"]].set()
            await release[args["which"]].wait()
            return ToolResult(content={"results": [{"title": "A local result"}]})

    registry = ToolRegistry(tmp_cfg)
    registry._register(FakeTool())
    ctx = CallContext("session-local", "task-local", "generation", "CreateInitialHypotheses")
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        jobs = [asyncio.create_task(_dispatch(registry, SimpleNamespace(
            name="local_search", input={"which": name, "query": "research private-test-key"},
        ), ctx, 5)) for name in ("one", "two")]
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered.values())), 2)
        data = await snapshot(client)
        assert len(data["agents"][0]["operations"]) == 2
        assert "private-test-key" not in str(data)
        release["one"].set()
        await jobs[0]
        assert len((await snapshot(client))["agents"][0]["operations"]) == 1
        release["two"].set()
        await jobs[1]
        data = await snapshot(client)
        assert data["agents"][0]["operations"] == []
        completed = [e for e in data["agents"][0]["events"] if e["event"] == "tool_completed"]
        assert len(completed) == 2
        assert all(e["payload"]["result_count"] == 1 for e in completed)


@pytest.mark.asyncio
async def test_tool_timeout_legacy_ownership_and_stopped_session(tmp_cfg, conn):
    await seed(conn, agent="reflection", task_id="review-local")
    await events.emit(conn, session_id="session-local", task_id=None, agent="supervisor",
                      event="task_started", payload={"task_id": "review-local"})
    registry = ToolRegistry(tmp_cfg)

    class SlowTool:
        name = "slow"

        async def call(self, args, ctx):
            await asyncio.Event().wait()

    registry._register(SlowTool())
    response = await _dispatch(registry, SimpleNamespace(name="slow", input={}),
                              CallContext("session-local", "review-local", "reflection", "ReviewHypothesis"), .01)
    assert response["is_error"]
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        data = await snapshot(client)
        reflection = data["agents"][1]
        assert reflection["operations"] == []
        assert any(e["event"] == "task_started" for e in reflection["events"])
        assert any(e["event"] == "tool_failed" for e in reflection["events"])
        await sessions.set_status(conn, "session-local", "aborted")
        data = await snapshot(client)
        assert data["agents"][1]["status"] == "stopped"
        assert data["agents"][1]["active_tasks"] == []
        assert (await client.get("/api/studio/sessions/missing/runtime")).status_code == 404


@pytest.mark.asyncio
async def test_observability_failure_does_not_break_model_call(tmp_cfg, conn, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(events, "emit", AsyncMock(side_effect=OSError("diagnostics unavailable")))

    class Provider:
        _cfg, _db = tmp_cfg, conn

        @observe_llm_call
        async def call(self, spec, ctx):
            return LLMResponse(raw=None, transcript_id="local", cost_usd=0,
                               input_tokens=0, output_tokens=0, cache_read=0, cache_write=0)

    result = await Provider().call(
        AgentCallSpec(route=ModelRoute(agent="generation", mode="literature", model="fake")),
        CallContext("local", None, "generation", "test"))
    assert result.transcript_id == "local"


@pytest.mark.asyncio
async def test_final_overview_has_task_events_and_start_time(tmp_cfg, conn, monkeypatch):
    from unittest.mock import AsyncMock

    from co_scientist.agents.base import AgentDeps
    from co_scientist.agents.metareview import MetaReviewAgent
    from co_scientist.agents.supervisor import Supervisor
    from co_scientist.models import TaskResult

    await seed(conn)
    await tasks.complete(conn, "task-local")
    monkeypatch.setattr(MetaReviewAgent, "execute", AsyncMock(return_value=TaskResult(
        kind="final_overview_generated", extra={"overview_path": "artifacts/test.md"})))
    session = await sessions.fetch(conn, "session-local")
    await Supervisor(tmp_cfg)._finalize(conn, AgentDeps(tmp_cfg, conn, None, ToolRegistry(tmp_cfg)),
                                       session, None)
    rows = [e for e in await events.recent(conn, session.id) if e["agent"] == "metareview"]
    assert {e["event"] for e in rows} == {"task_started", "task_completed"}
    async with conn.execute("SELECT started_at,status FROM tasks WHERE id=?", (rows[0]["task_id"],)) as cursor:
        final = await cursor.fetchone()
    assert final["started_at"] is not None
    assert final["status"] == "done"
