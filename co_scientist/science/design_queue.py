"""Bounded scientific requests, using the existing tasks table and Supervisor."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from .. import ids
from ..models import Task
from ..orchestrator.termination import budget_exceeded, wall_clock_exceeded
from ..storage import db
from ..storage.artifacts import _resolved_under, session_root
from ..storage.repos import hypotheses, sessions, tasks
from .alphaprotein_novo import AlphaProteinNovoAdapter


async def enqueue_design(cfg, session_id: str, hypothesis_id: str, raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    try:
        request = AlphaProteinNovoAdapter(cfg).validate_design_request(raw)
    except (ValueError, TypeError, OSError):
        return False
    # A separate transaction avoids interleaving BEGIN/COMMIT on the worker's
    # shared aiosqlite connection. SQLite serializes reservations across servers.
    conn = await db.connect(cfg)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        session = await sessions.fetch(conn, session_id)
        h = await hypotheses.fetch(conn, hypothesis_id)
        if session is None or h is None or h.session_id != session_id:
            return False
        if session.status == "aborted" or budget_exceeded(session) or wall_clock_exceeded(session):
            return False
        if request.parent_hypothesis_id and request.parent_hypothesis_id not in h.parent_ids:
            return False
        ancestors, pending = set(), list(h.parent_ids)
        while pending:
            hid = pending.pop()
            if hid == h.id:
                return False
            if hid in ancestors:
                continue
            parent = await hypotheses.fetch(conn, hid)
            if parent is None or parent.session_id != session_id:
                return False
            ancestors.add(hid)
            pending.extend(parent.parent_ids)
        async with conn.execute("SELECT target_id,payload FROM tasks WHERE session_id=? AND action='RunProteinDesign'", (session_id,)) as cur:
            history = await cur.fetchall()
        if len(history) >= cfg.alpha_protein_novo.max_design_tasks_per_session:
            return False
        # Stable across retries; a replay never means 'start another iteration'.
        key = f"{hypothesis_id}::protein_design::{request.mode}::{request.input_sha256}"
        async with conn.execute("SELECT 1 FROM tasks WHERE idempotency_key=?", (key,)) as cur:
            if await cur.fetchone():
                return False
        previous = [json.loads(r["payload"]).get("design_iteration", 1)
                    for r in history if r["target_id"] in ancestors | {h.id}]
        iteration = max([0, *previous]) + 1
        if iteration > cfg.alpha_protein_novo.max_design_iterations:
            return False
        tid = ids.task_id()
        # Per-task output prevents one model-generated path overwriting another
        # session or a previous campaign. Scientific input files are never made up.
        root = session_root(cfg, session_id)
        output = root / "scientific_tools" / "campaigns" / tid
        request.output_dir = str(_resolved_under(root, output))
        request.design_iteration = iteration
        request.parent_hypothesis_id = h.parent_ids[0] if h.parent_ids else None
        request.requires_approval = request.mode == "real"
        request.config_version = hashlib.sha256(cfg.alpha_protein_novo.model_dump_json().encode()).hexdigest()
        payload = request.model_dump(mode="json") | {"hypothesis_id": h.id, "parent_ids": h.parent_ids}
        return await tasks.enqueue(conn, Task(id=tid, session_id=session_id, created_at=datetime.now(UTC),
            agent="scientific_tool", action="RunProteinDesign", target_id=h.id, payload=payload,
            priority=115, status="awaiting_approval" if request.mode == "real" else "pending", idempotency_key=key))
    finally:
        # enqueue commits on success; reject/exception leaves no partial reservation.
        await conn.rollback()
        await conn.close()
