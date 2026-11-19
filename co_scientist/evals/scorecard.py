"""Fixed-goal quality protocol: scorecard plus optional live repeats.

The paper quality question is whether later hypotheses outrank earlier ones
under the same budget. Elo is an internal tournament ranking, not a lab result.
`session_scorecard` only reads a persisted session. `run_protocol` starts real
sessions and spends the configured budget.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import sessions as sess_repo
from ..storage.repos import tournaments as tourney_repo

FIXED_GOAL = "比较两种测量误差的可能来源"

PROTOCOL = {
    "goal": FIXED_GOAL,
    "repeats": 3,
    "research_mode": "lightweight",
    "n_initial": 3,
    "budget_usd": 2.0,
    "budget_tokens": 100000,
    "wall_clock_seconds": 600,
    "authors": [],
    "record": [
        "hypothesis_count", "match_count", "evolution_done",
        "meta_feedback", "later_outranks_early", "termination", "human_score",
    ],
    "note": (
        "Elo is internal ranking, not experimental effect. "
        "Repeats use the session provider only so three runs are comparable."
    ),
}


def apply_protocol(cfg: Config, data_dir: str | Path) -> Config:
    """Copy config into an isolated directory with the protocol knobs.

    Does not touch the caller's `data/` directory. Clears named generation
    authors so each repeat uses the same session model.
    """
    cfg = cfg.model_copy(deep=True)
    cfg.storage.data_dir = str(data_dir)
    cfg.run.research_mode = PROTOCOL["research_mode"]
    cfg.run.budget_usd = PROTOCOL["budget_usd"]
    cfg.run.budget_tokens = PROTOCOL["budget_tokens"]
    cfg.run.wall_clock_seconds = PROTOCOL["wall_clock_seconds"]
    cfg.generation.authors = list(PROTOCOL["authors"])
    return cfg


async def run_protocol(cfg: Config, *, repeats: int | None = None) -> dict[str, Any]:
    """Run the fixed goal `repeats` times. Calls real models and spends budget."""
    from ..agents.supervisor import Supervisor
    from ..storage import db as db_mod

    n = PROTOCOL["repeats"] if repeats is None else repeats
    if n < 1:
        raise ValueError("repeats must be >= 1")
    Path(cfg.storage.data_dir).mkdir(parents=True, exist_ok=True)
    await db_mod.init_db(cfg)
    runs: list[dict[str, Any]] = []
    for index in range(1, n + 1):
        started = datetime.now(UTC).isoformat()
        try:
            session_id = await Supervisor(cfg).run_session(
                PROTOCOL["goal"],
                n_initial=PROTOCOL["n_initial"],
                wall_clock_seconds=PROTOCOL["wall_clock_seconds"],
            )
            conn = await db_mod.connect(cfg)
            try:
                card = await session_scorecard(conn, session_id)
            finally:
                await conn.close()
            runs.append({
                "index": index, "started_at": started, "error": None,
                "scorecard": card,
            })
        except Exception as exc:
            runs.append({
                "index": index, "started_at": started,
                "error": {"type": type(exc).__name__, "message": str(exc)[:300]},
                "scorecard": None,
            })
    summary = {
        "protocol": PROTOCOL,
        "data_dir": str(cfg.data_dir),
        "finished_at": datetime.now(UTC).isoformat(),
        "runs": runs,
    }
    out = Path(cfg.storage.data_dir) / "scorecards.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


async def session_scorecard(conn, session_id: str) -> dict[str, Any] | None:
    session = await sess_repo.fetch(conn, session_id)
    if session is None:
        return None
    hyps = await hyp_repo.list_for_session(conn, session_id)
    matches = await tourney_repo.count_matches(conn, session_id)
    async with conn.execute(
        """SELECT COUNT(*) AS n FROM tasks
              WHERE session_id=? AND agent='evolution' AND status='done'""",
        (session_id,),
    ) as cur:
        evolution_done = (await cur.fetchone())["n"]
    async with conn.execute(
        """SELECT COUNT(*) AS n FROM system_feedback
              WHERE session_id=? AND kind='system_feedback' AND source='meta_review'""",
        (session_id,),
    ) as cur:
        meta_feedback = (await cur.fetchone())["n"]
    execution = (session.config_snapshot or {}).get("research_execution") or {}
    termination = (session.config_snapshot or {}).get("termination") or {}
    return {
        "session_id": session.id,
        "goal": session.research_goal,
        "status": session.status,
        "hypothesis_count": len(hyps),
        "match_count": matches,
        "evolution_done": evolution_done,
        "meta_feedback": meta_feedback,
        "later_outranks_early": later_outranks_early(hyps),
        "termination": termination,
        "literature_status": "recorded" if execution.get("literature_brief") else "none",
        "budget_used_usd": session.budget_used_usd,
        "budget_used_tokens": session.budget_used_tokens,
        "human_score": None,
        "note": PROTOCOL["note"],
        "protocol": PROTOCOL,
    }


def later_outranks_early(hypotheses) -> bool | None:
    """Whether the later half's max Elo beats the earlier half.

    Needs at least two scored hypotheses. Unscored rows do not count.
    """
    ordered = sorted(hypotheses, key=lambda h: (h.created_at, h.id))
    if len(ordered) < 2:
        return None
    mid = max(1, len(ordered) // 2)
    early = [h.elo for h in ordered[:mid] if h.elo is not None]
    late = [h.elo for h in ordered[mid:] if h.elo is not None]
    if not early or not late:
        return None
    return max(late) > max(early)
