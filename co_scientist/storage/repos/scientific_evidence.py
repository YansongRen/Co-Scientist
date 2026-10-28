"""Persistence helpers for the Scientific Evidence Layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite

from ...models.scientific_evidence import ScientificEvidence, valid_metric


def _unique_key(row: ScientificEvidence) -> tuple:
    """Identity used for agent-facing rendering, not database provenance."""
    return (
        row.session_id, row.task_id, row.hypothesis_id, row.design_id, row.design_iteration, row.kind, row.status,
        row.metric_name, json.dumps(row.metric_value, sort_keys=True, default=str),
        row.model_version, row.config_sha256, row.unit, row.evaluation_condition,
        json.dumps(row.uncertainty, sort_keys=True, default=str),
    )


async def insert_many(conn: aiosqlite.Connection, rows: list[ScientificEvidence]) -> None:
    if not rows:
        return
    for row in rows:
        async with conn.execute("SELECT session_id FROM tasks WHERE id=?", (row.task_id,)) as cur:
            task = await cur.fetchone()
        if task is None or task["session_id"] != row.session_id:
            raise ValueError("evidence task/session mismatch")
        for hid in [row.hypothesis_id, *row.parent_ids]:
            if not hid:
                continue
            async with conn.execute("SELECT session_id FROM hypotheses WHERE id=?", (hid,)) as cur:
                hypothesis = await cur.fetchone()
            if hypothesis is None or hypothesis["session_id"] != row.session_id:
                raise ValueError("evidence hypothesis/session mismatch")
        async with conn.execute("SELECT * FROM scientific_evidence WHERE id=?", (row.id,)) as cur:
            previous = await cur.fetchone()
        if previous:
            stored = _row_to_model(previous).model_dump(exclude={"created_at"})
            incoming = row.model_dump(exclude={"created_at"})
            if json.dumps(stored, sort_keys=True, default=str) != json.dumps(incoming, sort_keys=True, default=str):
                raise ValueError("scientific evidence is immutable; use a new task/version")
    await conn.executemany(
        """INSERT OR IGNORE INTO scientific_evidence(
           id, session_id, task_id, hypothesis_id, design_id, design_iteration, kind, status,
           metric_name, metric_value, unit, uncertainty, model_version,
           config_sha256, source_path, artifact_path, source_role, evaluation_condition, notes,
           parent_ids, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(
            row.id, row.session_id, row.task_id, row.hypothesis_id, row.design_id,
            row.design_iteration, row.kind, row.status, row.metric_name, json.dumps(row.metric_value),
            row.unit, json.dumps(row.uncertainty), row.model_version,
            row.config_sha256, row.source_path, row.artifact_path,
            row.source_role, row.evaluation_condition, row.notes, json.dumps(row.parent_ids),
            row.created_at.isoformat(),
        ) for row in rows],
    )
    await conn.commit()
    # A writer may have won after the preflight SELECT; detect ID conflicts.
    for incoming in rows:
        async with conn.execute("SELECT * FROM scientific_evidence WHERE id=?", (incoming.id,)) as cur:
            stored = _row_to_model(await cur.fetchone())
        if json.dumps(stored.model_dump(exclude={"created_at"}), sort_keys=True, default=str) != json.dumps(incoming.model_dump(exclude={"created_at"}), sort_keys=True, default=str):
            raise ValueError("scientific evidence is immutable; concurrent conflicting measurement")


async def list_for_hypothesis(
    conn: aiosqlite.Connection, hypothesis_id: str
) -> list[ScientificEvidence]:
    async with conn.execute(
        "SELECT * FROM scientific_evidence WHERE hypothesis_id=? ORDER BY created_at, id",
        (hypothesis_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_model(row) for row in rows]


async def list_for_task(
    conn: aiosqlite.Connection, task_id: str
) -> list[ScientificEvidence]:
    async with conn.execute(
        "SELECT * FROM scientific_evidence WHERE task_id=? ORDER BY created_at, id",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_model(row) for row in rows]


async def list_unique_for_hypotheses(
    conn: aiosqlite.Connection, hypothesis_ids: list[str],
) -> list[ScientificEvidence]:
    """Collapse JSON/CSV duplicates while retaining all rows in SQLite."""
    rows: list[ScientificEvidence] = []
    for hypothesis_id in hypothesis_ids:
        rows.extend(await list_for_hypothesis(conn, hypothesis_id))
    unique: dict[tuple, ScientificEvidence] = {}
    for row in rows:
        unique.setdefault(_unique_key(row), row)
    return list(unique.values())


async def render_for_agents(
    conn: aiosqlite.Connection,
    hypothesis_ids: list[str],
    *,
    include_mock: bool = True,
    include_invalid: bool = True,
) -> str:
    """Single evidence presentation layer for Reflection/Ranking/Evolution.

    Database rows remain append-only and preserve every source file. The
    rendered view deduplicates equivalent metrics and reports all source paths.
    ``include_mock=False`` is used by formal Ranking so simulated data cannot
    change candidate priority.
    """
    rows: list[ScientificEvidence] = []
    for hypothesis_id in hypothesis_ids:
        rows.extend(await list_for_hypothesis(conn, hypothesis_id))
    if not rows:
        return "No computational evidence is attached to these hypotheses."
    grouped: dict[tuple, list[ScientificEvidence]] = {}
    for row in rows:
        grouped.setdefault(_unique_key(row), []).append(row)
    measurements: dict[tuple, set[str]] = {}
    for row in rows:
        identity = (row.task_id, row.design_id, row.design_iteration, row.metric_name,
                    row.kind, row.status, row.unit, row.evaluation_condition)
        measurements.setdefault(identity, set()).add(json.dumps(row.metric_value, sort_keys=True))
    lines: list[str] = []
    skipped_mock = False
    skipped_invalid = False
    conflict_seen = False
    for group in grouped.values():
        row = group[0]
        if row.status == "mock" and not include_mock:
            skipped_mock = True
            continue
        numeric = row.kind in {"ap_novo_metric", "structure_prediction", "experimental", "mock"}
        identity = (row.task_id, row.design_id, row.design_iteration, row.metric_name,
                    row.kind, row.status, row.unit, row.evaluation_condition)
        conflict = len(measurements[identity]) > 1
        conflict_seen = conflict_seen or conflict
        invalid = conflict or row.status in {"unavailable", "error"} or (numeric and not valid_metric(row.metric_value))
        if invalid and not include_invalid:
            skipped_invalid = True
            continue
        sources = sorted({item.source_path for item in group if item.source_path})
        artifacts = sorted({item.artifact_path for item in group if item.artifact_path})
        validity = "invalid" if invalid else "simulation_only" if row.status == "mock" else "eligible"
        lines.append(
            f"- hypothesis={row.hypothesis_id}; evidence_id={row.id}; "
            f"task={row.task_id}; design={row.design_id or '?'}; iteration={row.design_iteration or '?'}; kind={row.kind}; metric={row.metric_name}; "
            f"value={row.metric_value!r}; unit={row.unit or '?'}; "
            f"status={row.status}; evidence_use={validity}; "
            f"model_version={row.model_version or '?'}; "
            f"condition={row.evaluation_condition or '?'}; uncertainty={row.uncertainty!r}; "
            f"source={' | '.join(sources)}; artifact={' | '.join(artifacts)}"
        )
    header = [
        "Persisted scientific evidence (deduplicated for agent reasoning).",
        "Computational structure/geometry evidence is not experimental catalytic activity.",
        "Mock values test software only; never use them to support a scientific claim or a real design decision.",
    ]
    if lines:
        header.extend(lines)
    if skipped_mock:
        header.append("- Mock evidence is present but excluded from formal ranking priority.")
    if skipped_invalid:
        header.append("- Error/unavailable evidence is present but excluded as invalid.")
    if conflict_seen:
        header.append("- Conflicting values from the same run are invalid until source files are reconciled.")
    return "\n".join(header)


def _decode(value: str | None) -> object:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _row_to_model(row: aiosqlite.Row) -> ScientificEvidence:
    row = dict(row)
    return ScientificEvidence(
        id=row["id"], session_id=row["session_id"], task_id=row["task_id"],
        hypothesis_id=row["hypothesis_id"], design_id=row["design_id"],
        design_iteration=row.get("design_iteration"),
        kind=row["kind"], status=row["status"], metric_name=row["metric_name"],
        metric_value=_decode(row["metric_value"]), unit=row["unit"],
        uncertainty=_decode(row["uncertainty"]), model_version=row["model_version"],
        config_sha256=row["config_sha256"], source_path=row["source_path"],
        artifact_path=row["artifact_path"], source_role=row["source_role"],
        evaluation_condition=row.get("evaluation_condition"),
        notes=row["notes"], parent_ids=_decode(row["parent_ids"]) or [],
        created_at=datetime.fromisoformat(row["created_at"]).replace(tzinfo=UTC),
    )
