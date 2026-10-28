"""Preserve ranked/unranked ordering on system SQLite used by ECS."""
from datetime import UTC, datetime, timedelta

import pytest

from co_scientist.models import Hypothesis, ResearchPlan, Session
from co_scientist.obs.metrics import leaderboard
from co_scientist.storage.repos import hypotheses, sessions


@pytest.mark.asyncio
async def test_ranked_and_unranked_hypotheses_keep_order_and_session_scope(conn):
    now = datetime.now(UTC)
    for session_id in ("ordering", "other"):
        await sessions.insert(conn, Session(
            id=session_id, created_at=now, updated_at=now, status="failed",
            research_goal="Offline sorting regression", research_plan=ResearchPlan(objective="Test"),
            config_snapshot={}, budget_tokens=1000, budget_usd=1,
        ))
    entries = [
        ("unranked-new", None, 4), ("zero", 0.0, 2),
        ("high-new", 1200.0, 3), ("negative", -1.0, 1),
        ("high-old", 1200.0, 0), ("unranked-old", None, -1),
    ]
    for hid, elo, age in entries + [("foreign", 9999.0, 0)]:
        await hypotheses.insert(conn, Hypothesis(
            id=hid, session_id="other" if hid == "foreign" else "ordering",
            created_at=now + timedelta(seconds=age), created_by="generation",
            strategy="literature", title=hid, summary="Test", full_text="Test",
            artifact_path="artifacts/missing.json", elo=elo, state="pinned",
        ))
    found = await hypotheses.list_for_session(conn, "ordering")
    assert [h.id for h in found] == [
        "high-old", "high-new", "zero", "negative", "unranked-old", "unranked-new",
    ]
    ranked = await leaderboard(conn, "ordering", k=6)
    assert [h.elo for h in ranked] == [1200.0, 1200.0, 0.0, -1.0, None, None]
    assert "foreign" not in {h.hypothesis_id for h in ranked}
    assert len(await leaderboard(conn, "ordering", k=3)) == 3
    assert await hypotheses.list_for_session(conn, "missing") == []
