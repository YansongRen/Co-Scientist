"""Offline regressions for system SQLite and visible startup failures."""
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from co_scientist.models import ResearchPlan, Session, Task
from co_scientist.storage.repos import events, sessions, tasks
from co_scientist.web.research_start import LaunchOptions, schedule_research


async def seed(conn, *, status="paused"):
    now = datetime.now(UTC)
    session = Session(id="queue-regression", created_at=now, updated_at=now,
        status=status, research_goal="Private offline research input",
        research_plan=ResearchPlan(objective="Test"), config_snapshot={},
        budget_tokens=10000, budget_usd=1)
    await sessions.insert(conn, session)
    await tasks.enqueue(conn, Task(id="queue-regression-task", session_id=session.id,
        created_at=now, agent="generation", action="CreateInitialHypotheses",
        payload={}, priority=100, status="pending", idempotency_key="queue-regression-key"))
    return session


@pytest.mark.asyncio
async def test_claim_works_without_sqlite_returning(conn, monkeypatch):
    session = await seed(conn)
    execute = conn.execute

    def legacy_execute(sql, *args, **kwargs):
        # Reproduce the server parser constraint even on newer local SQLite.
        if "RETURNING" in sql.upper():
            raise aiosqlite.OperationalError('near "RETURNING": syntax error')
        return execute(sql, *args, **kwargs)

    monkeypatch.setattr(conn, "execute", legacy_execute)
    claimed = await tasks.claim_one(conn, session.id, "worker-1", 60)
    assert claimed.id == "queue-regression-task"
    assert claimed.status == "leased" and claimed.lease_owner == "worker-1"
    assert await tasks.claim_one(conn, session.id, "worker-2", 60) is None


@pytest.mark.asyncio
async def test_failed_claim_releases_transaction_and_leaves_pending(conn, monkeypatch):
    session = await seed(conn)
    execute = conn.execute
    fail_once = True

    def failing_execute(sql, *args, **kwargs):
        nonlocal fail_once
        if fail_once and sql.lstrip().startswith("UPDATE tasks"):
            fail_once = False
            raise RuntimeError("Injected write failure")
        return execute(sql, *args, **kwargs)

    monkeypatch.setattr(conn, "execute", failing_execute)
    with pytest.raises(RuntimeError, match="Injected"):
        await tasks.claim_one(conn, session.id, "worker-1", 60)
    assert not conn.in_transaction
    assert await tasks.count_by_status(conn, session.id) == {"pending": 1}
    assert (await tasks.claim_one(conn, session.id, "worker-2", 60)).lease_owner == "worker-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "aborted"])
async def test_startup_failure_records_type_without_sensitive_message(tmp_cfg, conn, monkeypatch, status):
    session = await seed(conn, status=status)
    monkeypatch.setattr("co_scientist.agents.supervisor.Supervisor.run_session",
        AsyncMock(side_effect=RuntimeError("sk-not-for-logs Private offline research input")))
    app = SimpleNamespace(state=SimpleNamespace(background_runs={}))
    schedule_research(app, tmp_cfg, LaunchOptions(), session)
    await app.state.background_runs[session.id]
    await asyncio.sleep(0)
    saved = await sessions.fetch(conn, session.id)
    records = await events.recent(conn, session.id)
    assert "sk-not-for-logs" not in json.dumps(records)
    assert "Private offline research input" not in json.dumps(records)
    if status == "aborted":
        assert saved.status == "aborted" and not records
    else:
        assert saved.status == "failed"
        assert records[0]["event"] == "session_failed"
        assert records[0]["payload"] == {"error_type": "RuntimeError"}
    assert not app.state.background_runs
