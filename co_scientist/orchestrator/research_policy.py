"""Bounded scheduling for small research sessions, independent of model calls."""
from __future__ import annotations

import hashlib
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


def adaptive_maturity(cfg, hypotheses):
    required = min(cfg.evolution.min_mature, len(hypotheses))
    matches = min(3, cfg.ranking.lightweight_max_pair_matches)
    mature = sum(h.matches_played >= matches for h in hypotheses)
    return len(hypotheses) >= 2 and mature >= max(2, required)
