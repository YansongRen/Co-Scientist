"""Visitors register themselves. The administrator account is seeded."""
from __future__ import annotations

import httpx
import pytest

from co_scientist.tests.conftest import TEST_ADMIN_PASSWORD
from co_scientist.web.app import create_app


@pytest.mark.asyncio
async def test_login_page_offers_login_and_registration(tmp_cfg):
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/login")
    assert page.status_code == 200
    assert "登录" in page.text and "注册" in page.text
    assert "普通用户" not in page.text and "管理员登录" not in page.text
    assert 'name="role"' not in page.text
    assert "password_hash" not in page.text and "123456" not in page.text


@pytest.mark.asyncio
async def test_visitors_register_and_only_the_seeded_admin_opens_admin(tmp_cfg):
    transport = httpx.ASGITransport(app=create_app(tmp_cfg))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/register", data={"username": "研究员", "password": "research-pass"})
        assert created.status_code == 303 and created.headers["location"] == "/"
        home = await client.get("/")
        assert home.status_code == 200 and "研究员" in home.text and "普通用户" in home.text
        blocked = await client.get("/admin")
        assert blocked.status_code == 200 and "只对管理员开放" in blocked.text
        taken = await client.post("/register", data={"username": "rys", "password": "research-pass"})
        assert taken.status_code == 409 and "不能注册" in taken.text
        wrong = await client.post("/login", data={"username": "研究员", "password": "wrong-pass"})
        assert wrong.status_code == 401 and "账号或密码不正确" in wrong.text
        await client.post("/logout")
        signed_out = await client.get("/", follow_redirects=False)
        assert signed_out.status_code == 303 and signed_out.headers["location"] == "/login"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
        created = await admin.post("/login", data={"username": "rys", "password": TEST_ADMIN_PASSWORD})
        assert created.status_code == 303 and created.headers["location"] == "/admin"
        page = await admin.get("/admin")
        assert page.status_code == 200
        assert "研究员" in page.text and "rys" in page.text
        assert "pbkdf2" not in page.text and "123456" not in page.text
        workspace = await admin.get("/")
        assert workspace.status_code == 200 and "rys" in workspace.text
