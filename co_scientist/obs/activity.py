"""Small, durable execution events; never record prompts or model reasoning.

The ContextVar belongs to one async call, so concurrent agents/retries retain
their own correlation IDs. Observability failures must not fail research.
"""
from __future__ import annotations

import asyncio
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from uuid import uuid4

import aiosqlite

from ..config import Config
from ..logging import get_logger
from ..storage import db as db_mod
from ..storage.repos import events

log = get_logger("obs.activity")
_call_scope: ContextVar[tuple | None] = ContextVar("activity_call", default=None)
_task_scope: ContextVar[tuple | None] = ContextVar("activity_task", default=None)


@contextmanager
def activity_task(task):
    token = _task_scope.set((task.session_id, task.id, task.agent))
    try:
        yield
    finally:
        _task_scope.reset(token)


def _task_context(ctx):
    scope = _task_scope.get()
    if scope and ctx.task_id is None and (ctx.session_id, ctx.agent) == (scope[0], scope[2]):
        return replace(ctx, task_id=scope[1])
    return ctx


def safe_text(cfg: Config, value, *, limit: int = 300) -> str:
    text = str(value or "")
    for secret in cfg.secrets.model_dump().values():
        if isinstance(secret, str) and len(secret) >= 6:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|password|secret)=)[^&\s]+",
                  r"\1[redacted]", text)
    return text[:limit]


async def emit_activity(cfg, ctx, event: str, payload: dict, *, db=None) -> None:
    if not isinstance(cfg, Config) or not ctx.session_id:
        return
    ctx = _task_context(ctx)
    # Tool dispatch has no shared DB handle; use a short-lived connection.
    # Do not initialize a new database just to record diagnostics.
    owned = db is None
    if owned and not cfg.db_path.is_file():
        return
    if not owned and not isinstance(db, aiosqlite.Connection):
        return
    conn = None
    try:
        conn = await db_mod.connect(cfg) if owned else db
        owner = "supervisor" if ctx.agent == "parse_goal" else ctx.agent
        await events.emit(conn, session_id=ctx.session_id, task_id=ctx.task_id,
                          agent=owner, event=event, payload=payload)
    except Exception as error:
        log.warning("activity_write_failed", activity_event=event, error_type=type(error).__name__)
    finally:
        if owned and conn is not None:
            await conn.close()


def observe_llm_call(fn):
    @wraps(fn)
    async def observed(self, spec, ctx, **kwargs):
        ctx = _task_context(ctx)
        cfg, db = self._cfg, self._db
        # Eval callers without a session DB remain side-effect free.
        if db is None:
            return await fn(self, spec, ctx, **kwargs)
        span = uuid4().hex
        payload = {"span_id": span, "model": spec.route.model, "action": ctx.action}
        await emit_activity(cfg, ctx, "llm_call_started", payload, db=db)
        token = _call_scope.set((cfg, db, ctx, payload))
        started = time.monotonic()
        try:
            result = await fn(self, spec, ctx, **kwargs)
        except BaseException as error:
            name = "llm_call_cancelled" if isinstance(error, asyncio.CancelledError) else "llm_call_failed"
            await emit_activity(cfg, ctx, name, {
                **payload, "error_type": type(error).__name__,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }, db=db)
            raise
        else:
            await emit_activity(cfg, ctx, "llm_call_completed", {
                **payload, "transcript_id": result.transcript_id,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }, db=db)
            return result
        finally:
            _call_scope.reset(token)
    return observed


async def retry_sleep(delay: float, attempt: int, error: BaseException, *, sleep=None) -> None:
    """Expose backoff separately from time spent waiting for a model response."""
    scope = _call_scope.get()
    if scope:
        cfg, db, ctx, payload = scope
        await emit_activity(cfg, ctx, "llm_retry_wait", {
            **payload, "attempt": attempt, "delay_seconds": round(delay, 2),
            "error_type": type(error).__name__,
        }, db=db)
    await (sleep or asyncio.sleep)(delay)
    if scope:
        await emit_activity(cfg, ctx, "llm_retry_started", {
            **payload, "attempt": attempt + 1,
        }, db=db)
