"""One durable launch path shared by Direct Research and confirmed briefs."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .. import ids
from .. import inputs as user_inputs
from ..models import ResearchPlan, Session
from ..models.scoping import ResearchBrief
from ..logging import get_logger
from ..safety.evidence import infer_molecule
from ..storage import db
from ..storage.repos import events, sessions

log = get_logger("web.research_start")


class LaunchOptions(BaseModel):
    budget_usd: float = Field(1, gt=0, le=25, allow_inf_nan=False)
    budget_tokens: int | None = Field(None, ge=1000, le=10000000)
    upload_group_id: str | None = Field(None, pattern=r"^inp_[a-f0-9]{32}$")
    n_initial: int = Field(2, ge=1, le=20)
    wall_clock_seconds: int = Field(600, ge=60, le=7200)
    research_mode: Literal["standard", "lightweight"] = "lightweight"
    target_molecule: Literal["unspecified", "DNA", "RNA"] = "unspecified"

    @model_validator(mode="after")
    def lightweight_range(self):
        if self.research_mode == "lightweight" and not 2 <= self.n_initial <= 6:
            raise ValueError("轻量研究需要2至6条初始假设")
        return self


def config_for_launch(cfg, options):
    cfg = cfg.model_copy(deep=True)
    cfg.run.budget_usd = options.budget_usd
    if options.budget_tokens is not None:
        cfg.run.budget_tokens = options.budget_tokens
    cfg.run.wall_clock_seconds = options.wall_clock_seconds
    cfg.run.research_mode = options.research_mode
    cfg.run.target_molecule = options.target_molecule
    if options.research_mode == "lightweight":
        cfg.run.concurrency = 1
        cfg.run.max_ideas = min(cfg.run.max_ideas, 6)
    return cfg


async def prepare_research(conn, cfg, options, *, goal, preferences="", brief: ResearchBrief | None = None, draft_id=None):
    """Caller owns transaction: brief confirmation + Session insert are atomic."""
    run_cfg = config_for_launch(cfg, options)
    if brief:
        goal, preferences = brief.research_input()
    group_id = await user_inputs.group_for_draft(conn, draft_id) if draft_id else options.upload_group_id
    if draft_id and options.upload_group_id and options.upload_group_id != group_id:
        raise user_inputs.InputError("附件不属于当前讨论", 409)
    materials = await user_inputs.material_context(conn, cfg, group_id)
    image_ids = {image.attachment_id for image in await user_inputs.model_images(conn, cfg, group_id)}
    for material in materials:
        material["image_content_available"] = material["attachment_id"] in image_ids
    preferences += user_inputs.as_prompt(materials)
    if options.target_molecule != "unspecified":
        preferences += f"\nResearch scope: {options.target_molecule}; other molecule evidence is unverified extrapolation."
    molecule = options.target_molecule if options.target_molecule != "unspecified" else infer_molecule(goal)
    plan = ResearchPlan(objective=goal, target_molecule=molecule, preferences=[preferences] if preferences else [],
                        constraints=[f"[{c.source}] {c.text}" for c in brief.constraints] if brief else [],
                        notes=f"User-confirmed Research Brief; scoping draft {draft_id}" if brief else None)
    snap = json.loads(run_cfg.model_dump_json(exclude={"secrets"}))
    snap["web_launch"] = {"preferences": preferences, "confirmed_brief": brief.model_dump() if brief else None,
                          "scoping_id": draft_id, "n_initial": options.n_initial,
                          "upload_group_id": group_id, "attachments": [{k:v for k,v in m.items() if k != "text"} for m in materials]}
    now = datetime.now(UTC)
    session = Session(id=ids.session_id(), created_at=now, updated_at=now, status="paused",
        research_goal=goal, research_plan=plan, config_snapshot=snap,
        budget_tokens=run_cfg.run.budget_tokens, budget_usd=options.budget_usd,
        wall_deadline=now+timedelta(seconds=options.wall_clock_seconds))
    await sessions.insert(conn, session, commit=False)
    await user_inputs.link_group(conn, group_id, session.id, draft_id=draft_id)
    return session


def schedule_research(app, cfg, options, session):
    """No startup on GET/save/generate; invoked only by explicit launch POSTs."""
    from ..agents.supervisor import Supervisor

    async def run():
        try:
            await Supervisor(config_for_launch(cfg, options)).run_session(
                goal=session.research_goal, prepared_session_id=session.id,
                preferences_text=session.config_snapshot.get("web_launch", {}).get("preferences"),
                n_initial=options.n_initial, wall_clock_seconds=options.wall_clock_seconds)
        except Exception as exc:
            # Do not leak a Provider key, full configuration or research text.
            conn = await db.connect(cfg)
            try:
                changed = await conn.execute("UPDATE sessions SET status='failed' WHERE id=? AND status!='aborted'", (session.id,))
                await conn.commit()
                # Record only the class: exception messages can include Provider
                # credentials, uploaded research text or model response bodies.
                error_type = type(exc).__name__
                log.error("session_failed", session_id=session.id, error_type=error_type)
                if changed.rowcount:
                    await events.emit(conn, session_id=session.id, task_id=None,
                                      agent="supervisor", event="session_failed",
                                      payload={"error_type": error_type})
            finally:
                await conn.close()

    task = asyncio.create_task(run())
    app.state.background_runs[session.id] = task
    task.add_done_callback(lambda _: app.state.background_runs.pop(session.id, None))
