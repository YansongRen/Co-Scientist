"""Budget stop, bootstrap recovery, and web resume without hosted calls."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.supervisor import Supervisor
from co_scientist.llm.budgets import BudgetExceeded
from co_scientist.models import ResearchPlan, Session, Task
from co_scientist.orchestrator.termination import StabilityTracker, StopReason
from co_scientist.storage.repos import sessions, tasks
from co_scientist.web.app import create_app
from co_scientist.web.research_start import recover_running_research, resume_research


async def _session(conn, sid="rel-session", *, status="running"):
    now = datetime.now(UTC)
    session = Session(
        id=sid, created_at=now, updated_at=now, status=status,
        research_goal="Offline reliability", research_plan=ResearchPlan(objective="Offline reliability"),
        config_snapshot={"web_launch": {"n_initial": 3}, "run": {"concurrency": 1, "research_mode": "lightweight"}},
        budget_tokens=100000, budget_usd=1,
    )
    await sessions.insert(conn, session)
    return session


def _tracker():
    return StabilityTracker(5, 3, 25)


def _deps(cfg, conn):
    return AgentDeps(cfg, conn, None, None)


@pytest.mark.asyncio
async def test_budget_exceeded_dead_letters_without_retry_and_stops(tmp_cfg, conn, monkeypatch):
    session = await _session(conn)
    await tasks.enqueue(conn, Task(
        id="rel-task", session_id=session.id, created_at=datetime.now(UTC),
        agent="generation", action="CreateInitialHypotheses",
    ))
    execute = AsyncMock(side_effect=BudgetExceeded(
        "session token budget exhausted (used_tokens=90, reserved=0, need=20, cap=100)",
        kind="tokens",
    ))
    sup = Supervisor(tmp_cfg)
    monkeypatch.setattr(sup, "_build_agents", lambda deps: {"generation": SimpleNamespace(execute=execute)})
    decide = AsyncMock(return_value=0)
    monkeypatch.setattr(sup, "_decide_next_steps", decide)
    stop = await sup._main_loop(conn, _deps(tmp_cfg, conn), session, _tracker())
    assert stop == StopReason.BUDGET
    assert execute.await_count == 1
    decide.assert_not_awaited()
    assert await tasks.count_by_status(conn, session.id) == {"dead": 1}


@pytest.mark.asyncio
async def test_idle_without_hypotheses_records_no_output(tmp_cfg, conn, monkeypatch):
    session = await _session(conn, sid="rel-empty")
    monkeypatch.setattr(Supervisor, "_main_loop", AsyncMock(return_value=StopReason.IDLE))
    monkeypatch.setattr("co_scientist.agents.supervisor.get_provider", lambda *a, **k: None)
    await Supervisor(tmp_cfg).run_session("", resume_session_id=session.id)
    saved = await sessions.fetch(conn, session.id)
    term = saved.config_snapshot["termination"]
    assert saved.status == "done"
    assert term["reason"] == "idle"
    assert term["hypothesis_count"] == 0
    assert term["output"] == "none"
    assert "没有产生假设" in term["detail"]


@pytest.mark.asyncio
async def test_resume_enqueues_missing_initial_generation(tmp_cfg, conn, monkeypatch):
    session = await _session(conn, sid="rel-bootstrap", status="paused")
    monkeypatch.setattr(Supervisor, "_main_loop", AsyncMock(return_value=StopReason.EXTERNAL))
    monkeypatch.setattr("co_scientist.agents.supervisor.get_provider", lambda *a, **k: None)
    await Supervisor(tmp_cfg).run_session("", resume_session_id=session.id)
    async with conn.execute(
        "SELECT COUNT(*) n FROM tasks WHERE session_id=? AND agent='generation'", (session.id,),
    ) as cur:
        assert (await cur.fetchone())["n"] == 3
    await Supervisor(tmp_cfg).run_session("", resume_session_id=session.id)
    async with conn.execute(
        "SELECT COUNT(*) n FROM tasks WHERE session_id=? AND agent='generation'", (session.id,),
    ) as cur:
        assert (await cur.fetchone())["n"] == 3


@pytest.mark.asyncio
async def test_imported_session_resume_does_not_invent_generation(tmp_cfg, conn, monkeypatch):
    now = datetime.now(UTC)
    session = Session(
        id="rel-imported", created_at=now, updated_at=now, status="paused",
        research_goal="Imported", research_plan=ResearchPlan(objective="Imported"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1,
    )
    await sessions.insert(conn, session)
    monkeypatch.setattr(Supervisor, "_main_loop", AsyncMock(return_value=StopReason.EXTERNAL))
    monkeypatch.setattr("co_scientist.agents.supervisor.get_provider", lambda *a, **k: None)
    await Supervisor(tmp_cfg).run_session("", resume_session_id=session.id)
    assert await tasks.count_by_status(conn, session.id) == {}


@pytest.mark.asyncio
async def test_recover_running_research_starts_supervisor_once(tmp_cfg, conn, monkeypatch):
    session = await _session(conn)
    seen = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(*args, **kwargs):
        seen.append(kwargs)
        started.set()
        await release.wait()
        return session.id

    monkeypatch.setattr("co_scientist.agents.supervisor.Supervisor.run_session", fake_run)
    app = SimpleNamespace(state=SimpleNamespace(background_runs={}))
    assert await recover_running_research(app, tmp_cfg) == 1
    await started.wait()
    assert session.id in app.state.background_runs
    assert resume_research(app, tmp_cfg, session) is False
    assert await recover_running_research(app, tmp_cfg) == 0
    release.set()
    await app.state.background_runs[session.id]
    assert seen[0]["resume_session_id"] == session.id


@pytest.mark.asyncio
async def test_web_resume_starts_supervisor_when_none_is_live(tmp_cfg, conn, monkeypatch):
    session = await _session(conn, sid="rel-web", status="paused")
    seen = []

    async def fake_run(*args, **kwargs):
        seen.append(kwargs)
        return session.id

    monkeypatch.setattr("co_scientist.agents.supervisor.Supervisor.run_session", fake_run)
    app = create_app(tmp_cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session.id}/resume")
        assert response.status_code == 200
        assert response.json()["supervisor"] == "started"
        running = list(app.state.background_runs.values())
        if running:
            await asyncio.gather(*running)
    saved = await sessions.fetch(conn, session.id)
    assert saved.status == "running"


@pytest.mark.asyncio
async def test_web_resume_rejects_finished_session(tmp_cfg, conn, monkeypatch):
    session = await _session(conn, sid="rel-done", status="done")
    monkeypatch.setattr("co_scientist.agents.supervisor.Supervisor.run_session", AsyncMock())
    app = create_app(tmp_cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session.id}/resume")
        assert response.status_code == 409
        assert "不能恢复" in response.json()["detail"]
    saved = await sessions.fetch(conn, session.id)
    assert saved.status == "done"


@pytest.mark.asyncio
async def test_create_app_does_not_recover_running_by_default(tmp_cfg, conn, monkeypatch):
    session = await _session(conn, sid="rel-norecover", status="running")
    seen = []

    async def fake_run(*args, **kwargs):
        seen.append(kwargs)
        return session.id

    monkeypatch.setattr("co_scientist.agents.supervisor.Supervisor.run_session", fake_run)
    app = create_app(tmp_cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/healthz")).status_code == 200
    assert seen == []
    saved = await sessions.fetch(conn, session.id)
    assert saved.status == "running"


@pytest.mark.asyncio
async def test_studio_exposes_termination_without_config_snapshot(tmp_cfg, conn):
    session = await _session(conn, sid="rel-ui", status="done")
    session.config_snapshot["termination"] = {
        "reason": "budget", "hypothesis_count": 0, "output": "none",
        "detail": "预算不足，已停止；没有产生假设",
    }
    await conn.execute(
        "UPDATE sessions SET config_snapshot=? WHERE id=?",
        (json.dumps(session.config_snapshot), session.id),
    )
    await conn.commit()
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = (await client.get("/api/studio/sessions")).json()["sessions"][0]
        detail = (await client.get(f"/api/studio/sessions/{session.id}")).json()
    assert listing["stop_reason"] == "budget"
    assert listing["output"] == "none"
    assert listing["hypothesis_count"] == 0
    assert detail["session"]["stop_reason"] == "budget"
    assert detail["session"]["detail"].startswith("预算不足")
    assert "config_snapshot" not in detail["session"]
