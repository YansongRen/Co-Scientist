"""Admin API management: keys stay masked, overlay survives restart."""
from __future__ import annotations

import os

import httpx
import pytest

from co_scientist.config import Config, StorageCfg
from co_scientist.ops_providers import (
    OpsError,
    apply_ops_overlay,
    delete_endpoint,
    runtime_env_path,
    runtime_toml_path,
    set_api_key,
    snapshot,
    update_session,
    upsert_endpoint,
)
from co_scientist.web.app import create_app

SECRET = "sk-admin-test-secret-value-12345"


@pytest.mark.asyncio
async def test_only_admin_can_read_or_change_providers(tmp_cfg, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/admin/providers")).status_code == 401
        await client.post("/register", data={"username": "研究员", "password": "research-pass"})
        blocked = await client.get("/api/admin/providers")
        assert blocked.status_code == 403
        denied = await client.put("/api/admin/providers/keys", json={"name": "OPENAI_API_KEY", "value": SECRET})
        assert denied.status_code == 403
        page = await client.get("/admin")
        assert page.status_code == 200 and "只对管理员开放" in page.text
        assert "API 管理" not in page.text and SECRET not in page.text

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
        login = await admin.post("/login", data={"username": "rys", "password": "123456"})
        assert login.status_code == 303
        page = await admin.get("/admin")
        assert page.status_code == 200
        assert "API 管理" in page.text and "更换或新增 API Key" in page.text
        assert SECRET not in page.text and "pbkdf2" not in page.text
        saved = await admin.put("/api/admin/providers/keys", json={"name": "OPENAI_API_KEY", "value": SECRET})
        assert saved.status_code == 200
        body = saved.json()
        key = next(item for item in body["api_keys"] if item["name"] == "OPENAI_API_KEY")
        assert key["configured"] is True
        assert key["preview"] == "sk…2345"
        assert SECRET not in saved.text
        html = await admin.get("/admin")
        assert "已配置 sk…2345" in html.text
        assert SECRET not in html.text
        assert "123456" not in html.text
        form = await admin.post("/admin/providers/keys", data={"name": "TAVILY_API_KEY", "value": SECRET})
        assert form.status_code == 303 and "saved=key" in form.headers["location"]
        html = await admin.get("/admin")
        assert "TAVILY_API_KEY" in html.text and SECRET not in html.text
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("TAVILY_API_KEY", None)


@pytest.mark.asyncio
async def test_admin_can_add_model_endpoint_and_reload_overlay(tmp_cfg, monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tmp_cfg.llm.provider = "openai_compatible"
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
        await admin.post("/login", data={"username": "rys", "password": "123456"})
        created = await admin.put("/api/admin/providers/endpoints", json={
            "name": "kimi",
            "base_url": "https://api.moonshot.cn/v1",
            "model": "kimi-k3.1-pro",
            "api_key_env": "KIMI_API_KEY",
            "max_concurrent": 1,
            "api_key": SECRET,
        })
        assert created.status_code == 200
        assert created.json()["endpoints"][0]["name"] == "kimi"
        assert SECRET not in created.text
        session = await admin.put("/api/admin/providers/session", json={
            "provider": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "models": {"generation": "deepseek-flash"},
            "authors": ["kimi", "session"],
        })
        assert session.status_code == 200
        body = session.json()
        assert body["provider"] == "openai_compatible"
        assert body["base_url"] == "https://api.deepseek.com"
        assert body["models"]["generation"] == "deepseek-flash"
        assert body["authors"] == ["kimi", "session"]
        rejected = await admin.delete("/api/admin/providers/endpoints/kimi")
        assert rejected.status_code == 422
        assert "作者" in rejected.json()["detail"]

    toml_text = runtime_toml_path(tmp_cfg).read_text(encoding="utf-8")
    env_text = runtime_env_path(tmp_cfg).read_text(encoding="utf-8")
    assert SECRET not in toml_text
    assert "kimi-k3.1-pro" in toml_text
    assert "KIMI_API_KEY=" + SECRET in env_text
    assert oct(runtime_env_path(tmp_cfg).stat().st_mode & 0o777) == "0o600"

    fresh = Config(storage=StorageCfg(data_dir=str(tmp_cfg.data_dir)))
    fresh.secrets.KIMI_API_KEY = ""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    apply_ops_overlay(fresh)
    assert fresh.llm.provider == "openai_compatible"
    assert fresh.llm.openai.base_url == "https://api.deepseek.com"
    assert "kimi" in fresh.llm.endpoints
    assert fresh.llm.endpoints["kimi"].model == "kimi-k3.1-pro"
    assert fresh.generation.authors == ["kimi", "session"]
    assert fresh.models.generation == "deepseek-flash"
    assert fresh.secrets.KIMI_API_KEY == SECRET
    os.environ.pop("KIMI_API_KEY", None)


def test_overlay_helpers_reject_unsafe_values(tmp_cfg):
    with pytest.raises(OpsError, match="http"):
        upsert_endpoint(
            tmp_cfg, name="bad", base_url="ftp://example.com", model="x",
            api_key_env="KIMI_API_KEY",
        )
    with pytest.raises(OpsError, match="账号密码"):
        upsert_endpoint(
            tmp_cfg, name="bad", base_url="https://user:pass@api.example.com",
            model="x", api_key_env="KIMI_API_KEY",
        )
    with pytest.raises(OpsError, match="session"):
        upsert_endpoint(
            tmp_cfg, name="session", base_url="https://api.example.com",
            model="x", api_key_env="KIMI_API_KEY",
        )
    upsert_endpoint(
        tmp_cfg, name="kimi", base_url="https://api.moonshot.cn/v1",
        model="kimi-k3.1-pro", api_key_env="KIMI_API_KEY",
    )
    update_session(tmp_cfg, authors=["kimi"])
    with pytest.raises(OpsError, match="作者"):
        delete_endpoint(tmp_cfg, "kimi")
    preview = snapshot(tmp_cfg)
    set_api_key(tmp_cfg, "KIMI_API_KEY", "short")
    assert any(item["preview"] == "••••" for item in snapshot(tmp_cfg)["api_keys"] if item["name"] == "KIMI_API_KEY")
    assert preview["endpoints"][0]["name"] == "kimi"
    os.environ.pop("KIMI_API_KEY", None)
