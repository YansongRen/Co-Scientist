"""Derive a reconnectable Agent monitor from durable tasks and activity events."""
from __future__ import annotations

import json
import time

from ..obs.activity import safe_text

AGENTS = ("generation", "reflection", "ranking", "evolution", "proximity", "metareview")
FIELDS = frozenset({
    "span_id", "model", "action", "tool", "detail", "duration_ms", "result_count",
    "error", "error_type", "err", "attempt", "delay_seconds", "transcript_id",
    "input_tokens", "output_tokens", "cost_usd", "task_id", "target", "kind",
    "status", "iteration", "follow_hypothesis_ids", "review_ids", "match_ids",
    "label", "n_items", "hypothesis_ids", "mode", "strategy", "winner", "elo_a", "elo_b",
})


def _payload(cfg, raw):
    value = json.loads(raw) if raw else {}
    return {key: safe_text(cfg, item, limit=400) if isinstance(item, str) else item
            for key, item in value.items() if key in FIELDS}


async def runtime_snapshot(cfg, conn, session):
    async with conn.execute("""SELECT t.id,t.agent,t.action,t.target_id,t.status,t.attempts,
            t.last_error,t.created_at,t.started_at,t.finished_at,h.title AS target_title
            FROM tasks t LEFT JOIN hypotheses h ON h.id=t.target_id
            WHERE t.session_id=? ORDER BY COALESCE(t.started_at,t.created_at) DESC,t.id DESC""",
            (session.id,)) as cursor:
        tasks = [dict(row) for row in await cursor.fetchall()]
    for task in tasks:
        task["last_error"] = safe_text(cfg, task["last_error"])
    async with conn.execute("""SELECT CASE WHEN agent='parse_goal' THEN 'supervisor'
            ELSE agent END AS agent,COUNT(*) AS calls,
            SUM(input_tokens+output_tokens) AS tokens,SUM(cost_usd) AS cost_usd
            FROM transcripts WHERE session_id=? GROUP BY 1""", (session.id,)) as cursor:
        usage = {row["agent"]: dict(row) for row in await cursor.fetchall()}
    # Old Supervisor events stored the task ID inside JSON. Attribute those to
    # their real owner as well, so historical sessions remain useful.
    async with conn.execute("""WITH attributed AS (
            SELECT e.id,e.ts,e.event,e.payload,
                COALESCE(e.task_id,json_extract(e.payload,'$.task_id')) AS task_id,
                COALESCE(json_extract(e.payload,'$.agent'),t.agent,e.agent,'supervisor') AS owner
            FROM events e LEFT JOIN tasks t
                ON t.id=COALESCE(e.task_id,json_extract(e.payload,'$.task_id'))
            WHERE e.session_id=?
        ), ranked AS (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY owner ORDER BY id DESC) AS n
            FROM attributed
        ) SELECT * FROM ranked WHERE n<=20 ORDER BY id DESC""", (session.id,)) as cursor:
        recent = [{"id": row["id"], "ts": row["ts"], "event": row["event"],
                   "task_id": row["task_id"], "agent": row["owner"],
                   "payload": _payload(cfg, row["payload"])} for row in await cursor.fetchall()]
    operations = {}
    live = session.status in ("running", "paused")
    if live:
        # Correlation IDs handle parallel tool calls and multiple tasks for the
        # same agent. A completed span can never overwrite a different span.
        async with conn.execute("""SELECT id,ts,event,agent,task_id,payload FROM events
                WHERE session_id=? AND event IN (
                    'llm_call_started','llm_call_completed','llm_call_failed','llm_call_cancelled',
                    'llm_retry_wait','llm_retry_started',
                    'tool_started','tool_completed','tool_failed','tool_cancelled')
                AND (task_id IS NULL OR task_id IN (
                    SELECT id FROM tasks WHERE session_id=? AND status IN ('leased','in_progress')
                )) ORDER BY id""", (session.id, session.id)) as cursor:
            for row in await cursor.fetchall():
                payload = _payload(cfg, row["payload"])
                span = payload.get("span_id")
                if not span:
                    continue
                if row["event"] in ("llm_call_started", "tool_started"):
                    operations[span] = {"agent": row["agent"], "task_id": row["task_id"],
                        "started_at": row["ts"], "phase": row["event"], **payload}
                elif row["event"] in ("llm_retry_wait", "llm_retry_started"):
                    if span in operations:
                        operations[span].update(payload, phase=row["event"])
                else:
                    operations.pop(span, None)

    result = []
    for agent in (*AGENTS, "supervisor"):
        own_tasks = [task for task in tasks if task["agent"] == agent]
        active = [task for task in own_tasks if task["status"] in ("leased", "in_progress")]
        counts = {status: sum(task["status"] == status for task in own_tasks)
                  for status in ("pending", "awaiting_approval", "done", "dead", "failed", "cancelled")}
        own_ops = [op for op in operations.values() if op["agent"] == agent]
        if own_ops or (active and live):
            status = "running"
        elif session.status == "paused" and counts["pending"]:
            status = "paused"
        elif session.status == "running" and counts["awaiting_approval"]:
            status = "awaiting_approval"
        elif session.status == "running" and counts["pending"]:
            status = "queued"
        elif counts["dead"] or counts["failed"]:
            status = "failed"
        elif not live and (active or counts["pending"] or counts["cancelled"]):
            status = "stopped"
        elif counts["done"]:
            status = "idle" if live else "done"
        else:
            status = "idle" if live else "unused"
        result.append({"agent": agent, "status": status, "counts": counts,
            "step": next((e for e in recent if e["agent"] == agent and e["event"] == "agent_step"
                          and e["task_id"] in {t["id"] for t in active}), None),
            "retries": sum(task["attempts"] for task in own_tasks),
            "active_tasks": active if live else [], "last_task": own_tasks[0] if own_tasks else None,
            "operations": own_ops, "events": [e for e in recent if e["agent"] == agent],
            "usage": usage.get(agent, {"calls": 0, "tokens": 0, "cost_usd": 0})})
    return {"server_time": int(time.time() * 1000), "session_status": session.status,
            "agents": result[:6], "coordinator": result[6],
            "instrumented": any(e["event"].startswith(("llm_", "tool_")) for e in recent)}
