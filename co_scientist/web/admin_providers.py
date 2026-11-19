"""Admin-only routes for API keys, session models, and generation endpoints."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..config import Config
from ..ops_providers import (
    OpsError,
    delete_endpoint,
    set_api_key,
    snapshot,
    update_session,
    upsert_endpoint,
)
from ..storage import db as db_mod
from .auth import current_account

SAVED_MESSAGES = {
    "key": "密钥已保存。新研究会使用这把钥匙。",
    "session": "会话模型和作者已保存。",
    "endpoint": "生成端点已保存。",
    "deleted": "已删除这个生成端点。",
}


def admin_flash(request: Request) -> dict[str, str | None]:
    saved = request.query_params.get("saved", "")
    error = request.query_params.get("error", "").strip()
    return {
        "admin_notice": SAVED_MESSAGES.get(saved),
        "admin_error": error[:200] if error else None,
    }


def create_admin_providers_router(cfg: Config) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/providers")
    async def get_providers(request: Request):
        await _require_admin(request, cfg)
        return snapshot(cfg)

    @router.put("/api/admin/providers/keys")
    async def put_key(request: Request):
        await _require_admin(request, cfg)
        body = await _json_object(request)
        try:
            set_api_key(cfg, str(body.get("name") or ""), str(body.get("value") or ""))
        except OpsError as exc:
            raise HTTPException(422, str(exc)) from exc
        return snapshot(cfg)

    @router.put("/api/admin/providers/session")
    async def put_session(request: Request):
        await _require_admin(request, cfg)
        body = await _json_object(request)
        try:
            update_session(
                cfg,
                provider=body.get("provider"),
                base_url=body.get("base_url"),
                models=body.get("models"),
                authors=body.get("authors"),
            )
        except OpsError as exc:
            raise HTTPException(422, str(exc)) from exc
        return snapshot(cfg)

    @router.put("/api/admin/providers/endpoints")
    async def put_endpoint(request: Request):
        await _require_admin(request, cfg)
        body = await _json_object(request)
        try:
            upsert_endpoint(
                cfg,
                name=str(body.get("name") or ""),
                base_url=str(body.get("base_url") or ""),
                model=str(body.get("model") or ""),
                api_key_env=str(body.get("api_key_env") or ""),
                max_concurrent=body.get("max_concurrent", 1),
                api_key=body.get("api_key"),
            )
        except OpsError as exc:
            raise HTTPException(422, str(exc)) from exc
        return snapshot(cfg)

    @router.delete("/api/admin/providers/endpoints/{name}")
    async def remove_endpoint(request: Request, name: str):
        await _require_admin(request, cfg)
        try:
            delete_endpoint(cfg, name)
        except OpsError as exc:
            raise HTTPException(422, str(exc)) from exc
        return snapshot(cfg)

    @router.post("/admin/providers/keys")
    async def form_key(request: Request, name: str = Form(""), value: str = Form("")):
        if error := await _form_admin_error(request, cfg):
            return error
        try:
            set_api_key(cfg, name, value)
        except OpsError as exc:
            return _redirect(error=str(exc))
        return _redirect(saved="key")

    @router.post("/admin/providers/session")
    async def form_session(request: Request):
        if error := await _form_admin_error(request, cfg):
            return error
        form = await request.form()
        models = {str(key).removeprefix("model_"): str(form.get(key) or "")
                  for key in form if str(key).startswith("model_")}
        try:
            update_session(
                cfg,
                provider=str(form.get("provider") or ""),
                base_url=str(form.get("base_url") or ""),
                models=models,
                authors=str(form.get("authors") or ""),
            )
        except OpsError as exc:
            return _redirect(error=str(exc))
        return _redirect(saved="session")

    @router.post("/admin/providers/endpoints")
    async def form_endpoint(
        request: Request,
        name: str = Form(""),
        base_url: str = Form(""),
        model: str = Form(""),
        api_key_env: str = Form(""),
        max_concurrent: str = Form("1"),
        api_key: str = Form(""),
    ):
        if error := await _form_admin_error(request, cfg):
            return error
        try:
            upsert_endpoint(
                cfg,
                name=name,
                base_url=base_url,
                model=model,
                api_key_env=api_key_env,
                max_concurrent=max_concurrent,
                api_key=api_key or None,
            )
        except OpsError as exc:
            return _redirect(error=str(exc))
        return _redirect(saved="endpoint")

    @router.post("/admin/providers/endpoints/{name}/delete")
    async def form_delete_endpoint(request: Request, name: str):
        if error := await _form_admin_error(request, cfg):
            return error
        try:
            delete_endpoint(cfg, name)
        except OpsError as exc:
            return _redirect(error=str(exc))
        return _redirect(saved="deleted")

    return router


async def _require_admin(request: Request, cfg: Config) -> dict[str, str]:
    conn = await db_mod.connect(cfg)
    try:
        account = await current_account(request, cfg, conn)
    finally:
        await conn.close()
    if account is None:
        raise HTTPException(401, "请先登录")
    if account["role"] != "admin":
        raise HTTPException(403, "这个操作只对管理员开放")
    return account


async def _form_admin_error(request: Request, cfg: Config) -> RedirectResponse | None:
    conn = await db_mod.connect(cfg)
    try:
        account = await current_account(request, cfg, conn)
    finally:
        await conn.close()
    if account is None:
        return RedirectResponse("/login", status_code=303)
    if account["role"] != "admin":
        return RedirectResponse("/admin", status_code=303)
    return None


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "请提交 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(422, "请提交 JSON 对象")
    return body


def _redirect(*, saved: str | None = None, error: str | None = None) -> RedirectResponse:
    params: list[str] = []
    if saved:
        params.append(f"saved={quote(saved, safe='')}")
    if error:
        params.append(f"error={quote(error, safe='')}")
    query = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(f"/admin{query}#providers", status_code=303)
