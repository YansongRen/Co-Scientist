"""SQLite persistence/leases for the pre-Supervisor interactive drafts."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from ... import ids


def now():
    return datetime.now(UTC).isoformat()


async def create(conn, *, use_live=False, limits=None, cfg=None):
    draft_id = "scp_" + ids.session_id().removeprefix("ses_")
    await conn.execute("INSERT INTO scoping_drafts(id,created_at,updated_at,use_live,limits_json) VALUES(?,?,?,?,?)",
                       (draft_id, now(), now(), int(use_live), json.dumps(limits or {})))
    await conn.commit()
    return await fetch(conn, draft_id, cfg=cfg)


async def fetch(conn, draft_id, *, cfg=None):
    async with conn.execute("SELECT * FROM scoping_drafts WHERE id=?", (draft_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    d["messages"] = json.loads(d.pop("messages_json"))
    d["brief"] = json.loads(d.pop("brief_json") or "null")
    d["limits"] = json.loads(d.pop("limits_json") or "{}")
    from ... import inputs

    d["input_group_id"] = await inputs.group_for_draft(conn, draft_id)
    d["attachments"] = [inputs.public_file(cfg, r) for r in await inputs.list_files(conn, d["input_group_id"])]
    d["use_live"] = bool(d["use_live"])
    d["busy"] = bool(d["busy_call_id"] and (d["busy_until"] or 0) > time.time())
    async with conn.execute("""SELECT id,operation,status,model,error,input_tokens,output_tokens,cost_usd,
          reserved_tokens,reserved_usd,usage_known,created_at,finished_at,prompt_sha256,elapsed_seconds
          FROM scoping_calls WHERE draft_id=? ORDER BY created_at,id""", (draft_id,)) as cur:
        d["calls"] = [dict(r) for r in await cur.fetchall()]
    d["usage"] = {"calls": len(d["calls"]),
                  "tokens": sum(c["input_tokens"] + c["output_tokens"] if c["usage_known"] else c["reserved_tokens"] for c in d["calls"]),
                  "cost_usd": sum(c["cost_usd"] if c["usage_known"] else c["reserved_usd"] for c in d["calls"]),
                  "has_estimates": any(not c["usage_known"] for c in d["calls"])}
    d["usage"]["run_seconds"] = sum(c["elapsed_seconds"] for c in d["calls"])
    return d


async def record_provider_usage(conn, transcript):
    """Called by the existing API Providers after receiving a billable response."""
    cur = await conn.execute("""UPDATE scoping_calls SET input_tokens=?,output_tokens=?,cost_usd=?,
        usage_known=1,transcript_json=? WHERE id=? AND draft_id=?""",
        (transcript.input_tokens, transcript.output_tokens, transcript.cost_usd,
         transcript.model_dump_json(), transcript.task_id, transcript.session_id))
    if cur.rowcount != 1:
        raise ValueError("Scoping accounting target missing")
    await conn.commit()
