"""Preview reads local research without exposing configuration or unsafe HTML."""
from datetime import UTC, datetime

import httpx
import pytest

from co_scientist.models import Hypothesis, ResearchPlan, Session
from co_scientist.storage.repos import hypotheses, sessions
from co_scientist.web.app import create_app
from co_scientist.web.studio import _read_artifact


@pytest.mark.asyncio
async def test_workspace_reads_results_without_exposing_config(tmp_cfg, conn):
    now = datetime.now(UTC)
    report = tmp_cfg.data_dir / "artifacts" / "report.md"
    report.write_text('# Report\n\nA test result.\n\n<script>alert(1)</script>\n', encoding="utf-8")
    await sessions.insert(conn, Session(
        id="test-session", created_at=now, updated_at=now, status="done",
        research_goal="Test nanopore question", research_plan=ResearchPlan(objective="Test"),
        config_snapshot={"api_key": "secret-must-stay-on-server"},
        budget_tokens=1000, budget_usd=1, final_overview="artifacts/report.md",
    ))
    await hypotheses.insert(conn, Hypothesis(
        id="test-hypothesis", session_id="test-session", created_at=now,
        created_by="generation", strategy="literature", title="A hypothesis",
        summary="A falsifiable statement", full_text="**Evidence** <script>bad()</script>",
        artifact_path="artifacts/missing.json",
    ))
    before = await sessions.fetch(conn, "test-session")
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/")).status_code == 200
        listing = (await client.get("/api/studio/sessions")).json()
        assert listing["sessions"][0]["hypothesis_count"] == 1
        response = await client.get("/api/studio/sessions/test-session")
        assert response.status_code == 200
        assert "secret-must-stay-on-server" not in response.text
        assert "config_snapshot" not in response.json()["session"]
        assert "<script>" not in response.json()["report"]["html"]
        assert "<script>" not in response.json()["hypotheses"][0]["html"]
        assert len(response.json()["stages"]) == 6
        assert (await client.get("/api/studio/sessions/missing")).status_code == 404
    assert await sessions.fetch(conn, "test-session") == before


def test_preview_artifacts_cannot_escape_data_directory(tmp_cfg, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("private contents", encoding="utf-8")
    try:
        assert _read_artifact(tmp_cfg, str(outside)) == ""
        assert _read_artifact(tmp_cfg, str(outside), json_record=True) == {}
    finally:
        outside.unlink()
