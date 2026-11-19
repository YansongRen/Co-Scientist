"""System / human feedback repository."""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from ...models import SystemFeedback


async def insert(conn: aiosqlite.Connection, f: SystemFeedback) -> None:
    await conn.execute(
        """INSERT INTO system_feedback(
               id, session_id, created_at, source, kind, target_id, text,
               artifact_path, active)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            f.id, f.session_id, f.created_at.isoformat(),
            f.source, f.kind, f.target_id, f.text,
            f.artifact_path, 1 if f.active else 0,
        ),
    )
    await conn.commit()


async def active_for_session(
    conn: aiosqlite.Connection, session_id: str, target_id: str | None = None
) -> list[SystemFeedback]:
    """Return active feedback. If `target_id` is given, include targeted matches *plus* global."""
    if target_id:
        async with conn.execute(
            """SELECT * FROM system_feedback
                  WHERE session_id=? AND active=1
                    AND (target_id IS NULL OR target_id=?)
                  ORDER BY created_at DESC""",
            (session_id, target_id),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            """SELECT * FROM system_feedback
                  WHERE session_id=? AND active=1 AND target_id IS NULL
                  ORDER BY created_at DESC""",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_fb(r) for r in rows]


_PROMPT_LABELS = {
    ("human", "directive"): "Researcher directive",
    ("human", "preference"): "Researcher preference",
    ("human", "pin"): "Researcher pin note",
    ("human", "rejection"): "Researcher rejection note",
    ("meta_review", "system_feedback"): "Meta-review",
}


async def compose_prompt(
    conn: aiosqlite.Connection,
    session_id: str,
    *,
    target_id: str | None = None,
    limit_chars: int = 4000,
) -> str:
    """Active researcher notes plus the latest meta-review, for agent prompts.

    Pin/rejection without text stay state changes only. Directives and
    preferences are injected even when no meta-review exists yet.
    """
    rows = await active_for_session(conn, session_id, target_id=target_id)
    human: list[str] = []
    meta: str | None = None
    for fb in rows:
        text = (fb.text or "").strip()
        if not text:
            continue
        label = _PROMPT_LABELS.get((fb.source, fb.kind))
        if label is None:
            continue
        line = f"- {label}: {text}"
        if fb.source == "meta_review":
            if meta is None:
                meta = line
            continue
        human.append(line)
    parts = human
    if meta:
        parts.append(meta)
    if not parts:
        return ""
    body = "\n".join(parts)
    return body[:limit_chars]


async def latest_system_feedback(
    conn: aiosqlite.Connection, session_id: str
) -> SystemFeedback | None:
    async with conn.execute(
        """SELECT * FROM system_feedback
              WHERE session_id=? AND kind='system_feedback' AND source='meta_review'
              ORDER BY created_at DESC LIMIT 1""",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_fb(row) if row else None


async def deactivate(conn: aiosqlite.Connection, feedback_id: str) -> None:
    await conn.execute(
        "UPDATE system_feedback SET active=0 WHERE id=?", (feedback_id,)
    )
    await conn.commit()


def _row_to_fb(row: aiosqlite.Row) -> SystemFeedback:
    return SystemFeedback(
        id=row["id"],
        session_id=row["session_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        source=row["source"],
        kind=row["kind"],
        target_id=row["target_id"],
        text=row["text"],
        artifact_path=row["artifact_path"],
        active=bool(row["active"]),
    )
