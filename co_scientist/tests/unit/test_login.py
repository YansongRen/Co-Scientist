"""Local login separates an administrator from a regular researcher."""
from __future__ import annotations

import httpx
import pytest

from co_scientist.web.app import create_app


@pytest.mark.asyncio
async def test_login_page_has_user_and_admin_sections(tmp_cfg):
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/login")
    assert page.status_code == 200
    assert "普通用户" in page.text and "管理员" in page.text
    assert page.text.count('name="role"') == 2
    assert "password_hash" not in page.text


@pytest.mark.asyncio
async def test_each_role_enters_its_own_place_and_workspace_requires_login(tmp_cfg):
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/login", data={"role": "user", "username": "研究员", "password": "research-pass"})
        assert created.status_code == 303 and created.headers["location"] == "/"
        home = await client.get("/")
        assert home.status_code == 200 and "研究员" in home.text and "普通用户" in home.text
        blocked = await client.get("/admin")
        assert blocked.status_code == 200 and "只对管理员开放" in blocked.text
        wrong = await client.post("/login", data={"role": "user", "username": "研究员", "password": "wrong-pass"})
        assert wrong.status_code == 401 and "账号或密码不正确" in wrong.text
        await client.post("/logout")
        signed_out = await client.get("/", follow_redirects=False)
        assert signed_out.status_code == 303 and signed_out.headers["location"] == "/login"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
        created = await admin.post("/login", data={"role": "admin", "username": "管理员甲", "password": "admin-pass"})
        assert created.status_code == 303 and created.headers["location"] == "/admin"
        page = await admin.get("/admin")
        assert page.status_code == 200
        assert "研究员" in page.text and "管理员甲" in page.text
        assert "pbkdf2" not in page.text
        workspace = await admin.get("/")
        assert workspace.status_code == 200 and "管理员甲" in workspace.text
