"""Bounded scheduling for small research sessions, independent of model calls."""
from __future__ import annotations

import hashlib
import math
from itertools import combinations


async def eligible_pairs(conn, session_id, candidates, *, pair_cap=3, idea_cap=12):
    async with conn.execute("""SELECT hyp_a,hyp_b,COUNT(*) AS n FROM tournament_matches
            WHERE session_id=? GROUP BY MIN(hyp_a,hyp_b),MAX(hyp_a,hyp_b)""", (session_id,)) as cur:
        counts = {tuple(sorted((r["hyp_a"], r["hyp_b"]))): r["n"] for r in await cur.fetchall()}
    pairs = [(a, b) for a, b in combinations(candidates, 2)
             if counts.get(tuple(sorted((a.id, b.id))), 0) < pair_cap
             and a.matches_played < idea_cap and b.matches_played < idea_cap]
    return sorted(pairs, key=lambda pair: (
        counts.get(tuple(sorted((pair[0].id, pair[1].id))), 0),
        not bool(pair[0].dedup_cluster and pair[0].dedup_cluster == pair[1].dedup_cluster),
        pair[0].matches_played + pair[1].matches_played,
        abs((pair[0].elo or 1200) - (pair[1].elo or 1200)),
        sorted((pair[0].id, pair[1].id))))


async def pool_revision(conn, session_id, hypotheses):
    """Evolution/feedback run once per candidate pool and review revision."""
    async with conn.execute("SELECT id,body FROM reviews WHERE session_id=? ORDER BY id",
                            (session_id,)) as cur:
        reviews = [f"{r['id']}:{r['body']}" for r in await cur.fetchall()]
    body = "\n".join(sorted(h.id for h in hypotheses) + reviews)
    return hashlib.sha256(body.encode()).hexdigest()[:20]


def maturity_decision(cfg, hypotheses, *, session=None, n_initial=None, has_pairs=True):
    """Configurable small-pool gate; Elo separation is not scientific validity."""
    evo = cfg.evolution
    initial = n_initial or len(hypotheses)
    total_budget = session.budget_usd if session else cfg.run.budget_usd
    small = evo.small_pool_enabled and (initial <= evo.small_pool_max_initial or (
        0 < total_budget <= evo.small_pool_max_usd and len(hypotheses) <= evo.small_pool_max_initial))
    matches = evo.small_pool_matches if small else 3
    if small and cfg.run.research_mode == "lightweight":
        matches = min(matches, cfg.ranking.lightweight_max_pair_matches)
    reference_size = initial if initial <= evo.small_pool_max_initial else len(hypotheses)
    required = max(evo.small_pool_min_candidates, math.ceil(reference_size * evo.small_pool_mature_fraction)) if small else evo.min_mature
    mature = [h for h in hypotheses if h.matches_played >= matches and h.elo is not None]
    ranked = sorted(mature, key=lambda h: -h.elo)
    separated = len(ranked) >= 2 and ranked[0].elo - ranked[1].elo >= evo.small_pool_elo_gap
    budget_ok = True
    if session:
        remaining = evo.small_pool_min_remaining_fraction
        budget_ok = ((session.budget_usd <= 0 or session.budget_used_usd < session.budget_usd * (1-remaining))
            and (session.budget_tokens <= 0 or session.budget_used_tokens < session.budget_tokens * (1-remaining)))
    ready = (len(hypotheses) >= 2 and len(mature) >= required and budget_ok
             and (not small or not evo.small_pool_require_top_separation or separated))
    return {"ready": ready, "small_pool": small, "required_mature": required,
            "matches_per_mature": matches, "mature": len(mature), "has_eligible_pairs": has_pairs,
            "top_separated": separated, "budget_ok": budget_ok}


def adaptive_maturity(cfg, hypotheses):
    return maturity_decision(cfg, hypotheses)["ready"]
