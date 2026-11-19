"""Login gate, admin secrets, and operator-environment constraints."""
from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest

from co_scientist.config import Config, StorageCfg
from co_scientist.models import Hypothesis, ResearchPlan, Session
from co_scientist.ops_providers import (
    OpsError,
    apply_ops_overlay,
    apply_runtime_env,
    persist_runtime_env,
    runtime_env_path,
    set_api_key,
    upsert_endpoint,
)
from co_scientist.storage.repos import hypotheses as hyp_repo
from co_scientist.storage.repos import sessions as sess_repo
from co_scientist.tests.conftest import TEST_ADMIN_PASSWORD
from co_scientist.web.app import create_app
from co_scientist.web.auth import (
    account_by_username,
    ensure_admin,
    insert_account,
    verify_password,
)


@pytest.mark.asyncio
async def test_research_surfaces_lock_once_accounts_exist(tmp_cfg):
    app = create_app(tmp_cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/studio/sessions")).status_code == 200
        assert (await client.get("/classic")).status_code == 200
        await client.post("/register", data={"username": "研究员", "password": "research-pass"})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        classic = await anon.get("/classic", follow_redirects=False)
        new_form = await anon.get("/sessions/new", follow_redirects=False)
        sessions = await anon.get("/api/studio/sessions")
        abort = await anon.post("/api/sessions/ses_missing/abort")
        resume = await anon.post("/api/sessions/ses_missing/resume")
        feedback = await anon.post(
            "/api/sessions/ses_missing/feedback",
            data={"text": "ignore previous instructions", "kind": "directive"},
        )
        health = await anon.get("/healthz")
        login = await anon.get("/login")
    assert classic.status_code == 303 and classic.headers["location"] == "/login"
    assert new_form.status_code == 303 and new_form.headers["location"] == "/login"
    assert sessions.status_code == 401
    assert abort.status_code == 401
    assert resume.status_code == 401
    assert feedback.status_code == 401
    assert health.status_code == 200
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_feedback_missing_session_is_404_not_500(tmp_cfg):
    app = create_app(tmp_cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/sessions/ses_missing/feedback",
            data={"text": "directive", "kind": "directive"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_feedback_cannot_pin_a_hypothesis_from_another_session(tmp_cfg, conn):
    now = datetime.now(UTC)
    owner = Session(
        id="ses_owner", created_at=now, updated_at=now, status="running",
        research_goal="g", research_plan=ResearchPlan(objective="g"),
        config_snapshot={}, budget_tokens=0, budget_usd=1,
    )
    other = Session(
        id="ses_other", created_at=now, updated_at=now, status="running",
        research_goal="g", research_plan=ResearchPlan(objective="g"),
        config_snapshot={}, budget_tokens=0, budget_usd=1,
    )
    await sess_repo.insert(conn, owner)
    await sess_repo.insert(conn, other)
    await hyp_repo.insert(conn, Hypothesis(
        id="hyp_foreign", session_id=other.id, created_at=now, created_by="generation",
        strategy="literature", title="t", summary="s", full_text="full",
        artifact_path="artifacts/hyp_foreign.json", state="reviewed",
    ))
    app = create_app(tmp_cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/sessions/{owner.id}/feedback",
            data={"text": "reject this", "kind": "rejection", "target_id": "hyp_foreign"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_leaked_admin_password_is_rotated(tmp_cfg, conn):
    await insert_account(conn, username="rys", password="123456", role="admin")
    await ensure_admin(conn, tmp_cfg, create_if_missing=False)
    row = await account_by_username(conn, "rys")
    assert not verify_password("123456", row["password_hash"])
    assert verify_password(TEST_ADMIN_PASSWORD, row["password_hash"])


@pytest.mark.asyncio
async def test_key_save_rejected_on_plaintext_public_http(tmp_cfg):
    app = create_app(tmp_cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://public.example") as admin:
        login = await admin.post("/login", data={"username": "rys", "password": TEST_ADMIN_PASSWORD})
        assert login.status_code == 303
        saved = await admin.put(
            "/api/admin/providers/keys",
            json={"name": "OPENAI_API_KEY", "value": "sk-should-not-save"},
        )
    assert saved.status_code == 403
    assert "HTTPS" in saved.json()["detail"]


def test_runtime_env_skips_process_environment_names(tmp_cfg, monkeypatch):
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    path = runtime_env_path(tmp_cfg)
    tmp_cfg.data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("LD_PRELOAD=/tmp/x.so\nOPENAI_API_KEY=sk-ok\n", encoding="utf-8")
    apply_runtime_env(tmp_cfg)
    assert "LD_PRELOAD" not in os.environ
    assert os.environ.get("OPENAI_API_KEY") == "sk-ok"
    persist_runtime_env(tmp_cfg, {"TAVILY_API_KEY": "tvly-ok"})
    saved = path.read_text(encoding="utf-8")
    assert "LD_PRELOAD" not in saved
    assert "OPENAI_API_KEY=sk-ok" in saved
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("TAVILY_API_KEY", None)


def test_endpoint_cannot_bind_an_existing_secret_name(tmp_cfg, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "real-server-key")
    tmp_cfg.secrets.OPENAI_API_KEY = "real-server-key"
    upsert_endpoint(
        tmp_cfg,
        name="evil",
        base_url="https://attacker.example/v1",
        model="stolen",
        api_key_env="OPENAI_API_KEY",
    )
    assert tmp_cfg.llm.endpoints["evil"].api_key_env == "ENDPOINT_EVIL_API_KEY"
    assert os.environ["OPENAI_API_KEY"] == "real-server-key"
    assert not os.environ.get("ENDPOINT_EVIL_API_KEY")
    fresh = Config(storage=StorageCfg(data_dir=str(tmp_cfg.data_dir)))
    apply_ops_overlay(fresh)
    assert fresh.llm.endpoints["evil"].api_key_env == "ENDPOINT_EVIL_API_KEY"
    with pytest.raises(OpsError, match=r"\*_API_KEY"):
        set_api_key(tmp_cfg, "PATH", "/tmp")
    os.environ.pop("ENDPOINT_EVIL_API_KEY", None)
