"""Durable task queue repository.

Uses an immediate transaction for atomic claim, including system SQLite 3.26.
Lease TTL is set by the caller.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime

import aiosqlite

from ...models import Task


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _epoch_ms() -> int:
    return int(time.time() * 1000)


async def enqueue(conn: aiosqlite.Connection, task: Task) -> bool:
    """Insert a task; returns True on insert, False if idempotency_key collides."""
    try:
        await conn.execute(
            """INSERT INTO tasks(
                   id, session_id, created_at, agent, action, target_id, payload,
                   priority, status, attempts, idempotency_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.id,
                task.session_id,
                task.created_at.isoformat(),
                task.agent,
                task.action,
                task.target_id,
                json.dumps(task.payload),
                task.priority,
                task.status,
                task.attempts,
                task.idempotency_key,
            ),
        )
        await conn.commit()
        return True
    except aiosqlite.IntegrityError as exc:
        # Do not hide FK/data-integrity errors as a successful deduplication.
        if "UNIQUE constraint failed: tasks." in str(exc):
            await conn.commit()
            return False
        raise


async def claim_one(
    conn: aiosqlite.Connection, session_id: str, worker_id: str, lease_seconds: int
) -> Task | None:
    """Atomically claim the highest-priority pending task for a session."""
    expires_at = _epoch_ms() + lease_seconds * 1000
    started_at = _now()
    # BEGIN IMMEDIATE serializes selection and mutation across worker connections.
    # Do not use RETURNING: Alibaba Cloud Linux 3 ships SQLite 3.26.
    await conn.execute("BEGIN IMMEDIATE")
    try:
        async with conn.execute(
            """SELECT q.id FROM tasks q
                 WHERE q.session_id=? AND q.status='pending'
                   AND (q.action NOT IN ('RunTournamentBatch','EvolveTopHypotheses','UpdateProximityGraph')
                        OR NOT EXISTS (SELECT 1 FROM tasks active WHERE active.session_id=q.session_id
                            AND active.action=q.action AND active.status IN ('leased','in_progress')))
                 ORDER BY q.priority ASC, q.created_at ASC
                 LIMIT 1""",
            (session_id,),
        ) as cur:
            candidate = await cur.fetchone()
        row = None
        if candidate is not None:
            await conn.execute(
                """UPDATE tasks SET status='leased', lease_owner=?, lease_expires_at=?, started_at=?
                   WHERE id=? AND status='pending'""",
                (worker_id, expires_at, started_at, candidate["id"]),
            )
            async with conn.execute("SELECT * FROM tasks WHERE id=?", (candidate["id"],)) as cur:
                row = await cur.fetchone()
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    return _row_to_task(row) if row else None


async def heartbeat(conn: aiosqlite.Connection, task_id: str, lease_seconds: int) -> None:
    expires_at = _epoch_ms() + lease_seconds * 1000
    await conn.execute(
        "UPDATE tasks SET lease_expires_at=? WHERE id=? AND status IN ('leased','in_progress')",
        (expires_at, task_id),
    )
    await conn.commit()


async def mark_in_progress(conn: aiosqlite.Connection, task_id: str) -> None:
    await conn.execute(
        "UPDATE tasks SET status='in_progress',started_at=COALESCE(started_at,?) WHERE id=?",
        (_now(), task_id),
    )
    await conn.commit()


async def complete(conn: aiosqlite.Connection, task_id: str) -> None:
    await conn.execute(
        "UPDATE tasks SET status='done', finished_at=? WHERE id=?",
        (_now(), task_id),
    )
    await conn.commit()


async def approve(conn: aiosqlite.Connection, task_id: str) -> bool:
    """Release a human-reviewed scientific task into the normal queue."""
    async with conn.execute("SELECT payload FROM tasks WHERE id=? AND action='RunProteinDesign' AND status='awaiting_approval'", (task_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    payload = json.loads(row["payload"])
    from ...science.alphaprotein_novo import input_fingerprint
    digest = input_fingerprint(payload["manifest_path"])
    if payload.get("input_sha256") != digest:
        raise ValueError("scientific inputs changed; submit a new request for review")
    approval = approval_digest(payload, digest)
    cur = await conn.execute(
        "UPDATE tasks SET status='pending',approval_sha256=? WHERE id=? AND status='awaiting_approval' AND payload=?",
        (approval, task_id, row["payload"]),
    )
    changed = cur.rowcount > 0
    await conn.commit()
    return changed


def approval_digest(payload: dict, input_sha256: str) -> str:
    return hashlib.sha256((json.dumps(payload, sort_keys=True) + input_sha256).encode()).hexdigest()


async def fail(
    conn: aiosqlite.Connection,
    task_id: str,
    error: str,
    *,
    max_attempts: int = 3,
    requeue_backoff_seconds: list[int] | None = None,
) -> str:
    """Increment attempts; either requeue or dead-letter. Returns the final status."""
    _ = requeue_backoff_seconds  # backoff is enforced cooperatively by the scheduler (M5+)
    async with conn.execute(
        "SELECT attempts FROM tasks WHERE id=?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return "missing"
    attempts = row["attempts"] + 1
    if attempts >= max_attempts:
        await conn.execute(
            "UPDATE tasks SET status='dead', attempts=?, last_error=?, finished_at=? WHERE id=?",
            (attempts, error[:4000], _now(), task_id),
        )
        await conn.commit()
        return "dead"
    await conn.execute(
        """UPDATE tasks
              SET status='pending', attempts=?, last_error=?,
                  lease_owner=NULL, lease_expires_at=NULL,
                  started_at=NULL
            WHERE id=?""",
        (attempts, error[:4000], task_id),
    )
    await conn.commit()
    return "pending"


async def cancel_pending_for_session(conn: aiosqlite.Connection, session_id: str) -> int:
    cur = await conn.execute(
        "UPDATE tasks SET status='cancelled' WHERE session_id=? AND status='pending'",
        (session_id,),
    )
    n = cur.rowcount
    await conn.commit()
    return n


async def reclaim_expired_leases(
    conn: aiosqlite.Connection,
    session_id: str | None = None,
    *,
    max_attempts: int = 3,
) -> dict[str, int]:
    """On startup: reset expired-lease tasks; dead-letter those past max_attempts.

    Returns {"requeued": n, "dead": m}.
    """
    now_ms = _epoch_ms()
    sess_clause = " AND session_id=?" if session_id else ""
    base_args: tuple = (session_id,) if session_id else ()

    # First: those that would exceed max_attempts → dead-letter.
    sql_dead = f"""UPDATE tasks
                      SET status='dead',
                          attempts=attempts+1,
                          finished_at=?,
                          last_error=COALESCE(last_error,'') || ' [lease expired; max attempts]'
                    WHERE status IN ('leased','in_progress')
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < ?
                      AND attempts + 1 >= ?
                      {sess_clause}"""
    cur = await conn.execute(sql_dead, (_now(), now_ms, max_attempts, *base_args))
    n_dead = cur.rowcount

    # Then: requeue the rest.
    sql_req = f"""UPDATE tasks
                     SET status='pending',
                         lease_owner=NULL,
                         lease_expires_at=NULL,
                         started_at=NULL,
                         attempts=attempts+1,
                         last_error=COALESCE(last_error,'') || ' [lease expired]'
                   WHERE status IN ('leased','in_progress')
                     AND lease_expires_at IS NOT NULL
                     AND lease_expires_at < ?
                     {sess_clause}"""
    cur = await conn.execute(sql_req, (now_ms, *base_args))
    n_req = cur.rowcount
    await conn.commit()
    return {"requeued": n_req, "dead": n_dead}


async def count_by_status(
    conn: aiosqlite.Connection, session_id: str
) -> dict[str, int]:
    async with conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE session_id=? GROUP BY status",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
    return {r["status"]: r["n"] for r in rows}


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        session_id=row["session_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        agent=row["agent"],
        action=row["action"],
        target_id=row["target_id"],
        payload=json.loads(row["payload"]) if row["payload"] else {},
        priority=row["priority"],
        status=row["status"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        idempotency_key=row["idempotency_key"],
    )
