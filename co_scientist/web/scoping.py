"""Draft API. Drafts never start scientific work until /confirm is called."""
# ruff: noqa: RUF001 -- Chinese user-facing errors.
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import inputs
from ..llm.provider import API_PROVIDERS
from ..models.scoping import ResearchBrief
from ..models.scoping_limits import ScopingLimits, check_limits, effective_limits
from ..scoping import ResearchScopingAssistant, ScopingError
from ..storage import db
from ..storage.repos import scoping as repo
from .research_start import LaunchOptions, prepare_research, schedule_research


class NewDraft(BaseModel):
    use_live: bool | None = None  # legacy explicit override; UI uses server mode
    limits: ScopingLimits | None = None


class SaveLimits(BaseModel):
    limits: ScopingLimits
    version: int = Field(ge=0)


class DraftOperation(BaseModel):
    operation: Literal["message", "regenerate", "brief"] = "message"
    text: str = Field("", max_length=4000)
    request_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=0)


class SaveBrief(BaseModel):
    brief: ResearchBrief
    version: int = Field(ge=0)


class ConfirmBrief(SaveBrief):
    confirmed: Literal[True]
    options: LaunchOptions = Field(default_factory=LaunchOptions)


def create_scoping_router(cfg):
    router = APIRouter(prefix="/api/scoping")

    @router.get("")
    async def listing():
        conn = await db.connect(cfg)
        try:
            async with conn.execute("SELECT id,updated_at,linked_session_id,use_live,brief_json,messages_json FROM scoping_drafts ORDER BY updated_at DESC LIMIT 50") as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            for row in rows:
                brief = json.loads(row.pop("brief_json") or "null")
                messages = json.loads(row.pop("messages_json"))
                row["title"] = brief["topic"] if brief else next((m["content"][:40] for m in messages if m["role"] == "user"), "新的研究想法")
            return {"drafts": rows, "allow_live_calls": cfg.scoping.allow_live_calls and cfg.llm.provider in API_PROVIDERS,
                    "limits": cfg.scoping.model_dump(exclude={"model"}),
                    "defaults": effective_limits(cfg).model_dump(),
                    "ceilings": {"budget_usd": cfg.scoping.max_budget_usd,"max_tokens":cfg.scoping.max_budget_tokens,"wall_clock_seconds":cfg.scoping.max_run_seconds},
                    "model": (cfg.scoping.model or cfg.models.parse_goal) if cfg.scoping.allow_live_calls else "Mock",
                    "uploads": cfg.uploads.model_dump()}
        finally:
            await conn.close()

    @router.post("")
    async def create(body: NewDraft):
        use_live = body.use_live if body.use_live is not None else cfg.scoping.allow_live_calls and cfg.llm.provider in API_PROVIDERS
        if use_live and (not cfg.scoping.allow_live_calls or cfg.llm.provider not in API_PROVIDERS):
            raise HTTPException(403, "服务端未启用引导模型 API 调用")
        try:
            limits = check_limits(cfg, body.limits or effective_limits(cfg))
        except ValueError:
            raise HTTPException(422, "预算或 Token 上限超过服务端限额") from None
        conn = await db.connect(cfg)
        try:
            return await repo.create(conn, use_live=use_live, limits=limits.model_dump(), cfg=cfg)
        finally:
            await conn.close()

    @router.get("/{draft_id}")
    async def get_draft(draft_id: str):
        conn = await db.connect(cfg)
        try:
            draft = await repo.fetch(conn, draft_id, cfg=cfg)
            if not draft:
                raise HTTPException(404, "引导会话不存在")
            return draft
        finally:
            await conn.close()

    @router.put("/{draft_id}/limits")
    async def save_limits(draft_id: str, body: SaveLimits):
        conn = await db.connect(cfg)
        try:
            check_limits(cfg, body.limits)
            await conn.execute("BEGIN IMMEDIATE")
            draft = await repo.fetch(conn, draft_id, cfg=cfg)
            if not draft:
                raise HTTPException(404, "讨论不存在")
            if draft["busy"] or draft["linked_session_id"] or draft["version"] != body.version:
                raise HTTPException(409, "讨论状态已变化")
            if body.limits.budget_tokens < draft["usage"]["tokens"] or body.limits.budget_usd < draft["usage"]["cost_usd"] or body.limits.wall_clock_seconds < draft["usage"]["run_seconds"]:
                raise HTTPException(422, "上限不能低于已使用量")
            await conn.execute("UPDATE scoping_drafts SET limits_json=?,version=version+1,updated_at=? WHERE id=?", (body.limits.model_dump_json(), repo.now(), draft_id))
            await conn.commit()
            return await repo.fetch(conn, draft_id, cfg=cfg)
        except ValueError:
            raise HTTPException(422, "预算或 Token 上限超过服务端限额") from None
        finally:
            await conn.rollback()
            await conn.close()

    @router.post("/{draft_id}/turns")
    async def turn(draft_id: str, body: DraftOperation, request: Request):
        try:
            return await ResearchScopingAssistant(cfg, getattr(request.app.state, "scoping_provider_factory", None)).operate(
                draft_id, operation=body.operation, text=body.text, request_id=body.request_id, expected_version=body.version)
        except ScopingError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    async def save(draft_id, body, request=None):
        conn = await db.connect(cfg)
        session = None
        try:
            await conn.execute("BEGIN IMMEDIATE")
            draft = await repo.fetch(conn, draft_id)
            if not draft:
                raise HTTPException(404, "引导会话不存在")
            if request and draft["linked_session_id"]:
                await conn.rollback()
                return {"session_id": draft["linked_session_id"], "already_confirmed": True}
            if draft["busy"] or draft["linked_session_id"] or draft["version"] != body.version:
                raise HTTPException(409, "任务书已更新或正在回复，请刷新后继续")
            if request and not draft["brief"]:
                raise HTTPException(409, "请先生成并预览研究任务书")
            if request:
                session = await prepare_research(conn, cfg, body.options, goal="", brief=body.brief, draft_id=draft_id)
            await conn.execute("UPDATE scoping_drafts SET brief_json=?,version=version+1,updated_at=?,linked_session_id=? WHERE id=?",
                               (body.brief.model_dump_json(), repo.now(), session.id if session else None, draft_id))
            await conn.commit()
            if session:
                schedule_research(request.app, cfg, body.options, session)
                return {"session_id": session.id, "already_confirmed": False}
            return await repo.fetch(conn, draft_id, cfg=cfg)
        except inputs.InputError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            await conn.rollback()
            await conn.close()

    @router.put("/{draft_id}/brief")
    async def save_brief(draft_id: str, body: SaveBrief):
        return await save(draft_id, body)

    @router.post("/{draft_id}/confirm")
    async def confirm(draft_id: str, body: ConfirmBrief, request: Request):
        return await save(draft_id, body, request)

    return router
