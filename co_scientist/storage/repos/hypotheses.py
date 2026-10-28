"""Hypothesis repository."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite

from ...models import Hypothesis


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def insert(conn: aiosqlite.Connection, h: Hypothesis) -> bool:
    """Insert; returns True if a new row was created, False if it already existed.

    Because hypothesis IDs are deterministic (sha256 of normalized statement),
    re-runs are safe via INSERT OR IGNORE.
    """
    cur = await conn.execute(
        """INSERT OR IGNORE INTO hypotheses(
               id, session_id, created_at, created_by, strategy, parent_ids,
               title, summary, full_text, artifact_path,
               elo, matches_played, state, dedup_cluster)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            h.id,
            h.session_id,
            h.created_at.isoformat(),
            h.created_by,
            h.strategy,
            json.dumps(h.parent_ids),
            h.title,
            h.summary,
            h.full_text,
            h.artifact_path,
            h.elo,
            h.matches_played,
            h.state,
            h.dedup_cluster,
        ),
    )
    inserted = cur.rowcount > 0
    await conn.commit()
    return inserted


async def fetch(conn: aiosqlite.Connection, hypothesis_id: str) -> Hypothesis | None:
    async with conn.execute("SELECT * FROM hypotheses WHERE id = ?", (hypothesis_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_hyp(row) if row else None


async def fetch_many(conn: aiosqlite.Connection, ids: list[str]) -> list[Hypothesis]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    async with conn.execute(
        f"SELECT * FROM hypotheses WHERE id IN ({placeholders})", tuple(ids)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_hyp(r) for r in rows]


async def list_for_session(
    conn: aiosqlite.Connection, session_id: str, state: str | None = None
) -> list[Hypothesis]:
    if state:
        async with conn.execute(
            "SELECT * FROM hypotheses WHERE session_id=? AND state=? ORDER BY elo DESC",
            (session_id, state),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            "SELECT * FROM hypotheses WHERE session_id=? ORDER BY (elo IS NULL), elo DESC, created_at",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_hyp(r) for r in rows]


async def top_by_elo(
    conn: aiosqlite.Connection, session_id: str, k: int = 10
) -> list[Hypothesis]:
    async with conn.execute(
        """SELECT * FROM hypotheses
              WHERE session_id=? AND state IN ('in_tournament','pinned')
              ORDER BY elo DESC LIMIT ?""",
        (session_id, k),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_hyp(r) for r in rows]


async def set_state(conn: aiosqlite.Connection, hypothesis_id: str, state: str) -> None:
    await conn.execute(
        "UPDATE hypotheses SET state=? WHERE id=?",
        (state, hypothesis_id),
    )
    await conn.commit()


async def set_state_if(
    conn: aiosqlite.Connection,
    hypothesis_id: str,
    *,
    new_state: str,
    expected_states: tuple[str, ...],
) -> bool:
    """Conditional state transition. Returns True if applied.

    Used to keep agents from clobbering a downstream state — e.g. Reflection
    re-running on an already-`in_tournament` hypothesis must not drag it
    back to `reviewed`.
    """
    placeholders = ",".join("?" for _ in expected_states)
    cur = await conn.execute(
        f"UPDATE hypotheses SET state=? WHERE id=? AND state IN ({placeholders})",
        (new_state, hypothesis_id, *expected_states),
    )
    changed = cur.rowcount > 0
    await conn.commit()
    return changed


async def init_tournament(
    conn: aiosqlite.Connection, hypothesis_id: str, initial_elo: float = 1200.0
) -> bool:
    """Set Elo and state if not already in tournament; returns True if applied."""
    cur = await conn.execute(
        """UPDATE hypotheses
              SET elo=?, state='in_tournament'
            WHERE id=? AND elo IS NULL""",
        (initial_elo, hypothesis_id),
    )
    changed = cur.rowcount > 0
    await conn.commit()
    return changed


async def set_dedup_cluster(
    conn: aiosqlite.Connection, hypothesis_id: str, cluster: str | None
) -> None:
    await conn.execute(
        "UPDATE hypotheses SET dedup_cluster=? WHERE id=?",
        (cluster, hypothesis_id),
    )
    await conn.commit()


def _row_to_hyp(row: aiosqlite.Row) -> Hypothesis:
    parent_ids = json.loads(row["parent_ids"]) if row["parent_ids"] else []
    return Hypothesis(
        id=row["id"],
        session_id=row["session_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        created_by=row["created_by"],
        strategy=row["strategy"],
        parent_ids=parent_ids,
        title=row["title"],
        summary=row["summary"],
        full_text=row["full_text"],
        citations=[],  # citations live in the JSON artifact, not the row
        artifact_path=row["artifact_path"],
        elo=row["elo"],
        matches_played=row["matches_played"],
        state=row["state"],
        dedup_cluster=row["dedup_cluster"],
    )
