"""SQLite connection helpers and migration runner.

Single connection per process is fine for our workload (writes serialize on the WAL
anyway). We use aiosqlite for the async API and set PRAGMAs on each connect.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from ..config import Config

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def _apply_pragmas(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA busy_timeout=5000;")
    deadline = time.monotonic() + 5
    while True:
        try:
            await conn.execute("PRAGMA journal_mode=WAL;")
            break
        except aiosqlite.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.02)
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    await conn.commit()


async def connect(cfg: Config) -> aiosqlite.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(cfg.db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await _apply_pragmas(conn)
    except BaseException:
        await conn.close()
        raise
    return conn


async def init_db(cfg: Config) -> None:
    """Create the DB if missing and apply pending migrations."""
    conn = await connect(cfg)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, name TEXT NOT NULL)")
        await _apply_pending(conn)
        await conn.commit()
    finally:
        await conn.rollback()
        await conn.close()


async def _ensure_migrations_table(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL,
               name       TEXT NOT NULL
           )"""
    )
    await conn.commit()


async def _applied_versions(conn: aiosqlite.Connection) -> set[int]:
    async with conn.execute("SELECT version FROM schema_migrations") as cur:
        rows = await cur.fetchall()
    return {row["version"] for row in rows}


def _discover_migrations() -> list[tuple[int, str, Path]]:
    """Return [(version, name, path)] sorted by version."""
    out: list[tuple[int, str, Path]] = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        # macOS archives may include ._*.sql AppleDouble sidecars.
        if p.name.startswith("."):
            continue
        stem = p.stem  # e.g. 0001_initial
        version_str, _, name = stem.partition("_")
        out.append((int(version_str), name or "unnamed", p))
    return out


async def _apply_pending(conn: aiosqlite.Connection) -> None:
    applied = await _applied_versions(conn)
    for version, name, path in _discover_migrations():
        if version in applied:
            continue
        # v1 special-case: load the canonical schema.sql to avoid duplication.
        sql = SCHEMA_PATH.read_text() if version == 1 else path.read_text()
        statement = ""
        for line in sql.splitlines(keepends=True):
            statement += line
            if not sqlite3.complete_statement(statement):
                continue
            try:
                await conn.execute(statement)
            except aiosqlite.OperationalError as e:
                # The canonical fresh schema may already include an ALTER's column.
                msg = str(e).lower()
                if "duplicate column name" not in msg:
                    raise
            statement = ""
        await conn.execute(
            "INSERT INTO schema_migrations(version, applied_at, name) VALUES (?, ?, ?)",
            (version, datetime.now(UTC).isoformat(), name),
        )


async def table_names(conn: aiosqlite.Connection) -> list[str]:
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cur:
        rows = await cur.fetchall()
    return [r["name"] for r in rows]
