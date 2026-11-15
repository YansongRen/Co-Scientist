"""Schedule generation and evolution from match results.

Weights are a scheduling bias. They are not a probability that a hypothesis
is true. With fewer than two measured options, the configured order is unchanged.
"""
from __future__ import annotations

import hashlib


def prefer_effective(configured: list[str], mean_elo: dict[str, float]) -> list[str]:
    known = [name for name in configured if name in mean_elo]
    if len(known) < 2:
        return list(configured)
    original = {name: index for index, name in enumerate(configured)}
    return sorted(configured, key=lambda name: (-float(mean_elo[name]) if name in mean_elo else 0.0, original[name]))


def scheduling_weight(mean_elo: float) -> float:
    """Map an Elo average onto a positive sampling weight.

    1000 is below the 1200 starting rating, so a strategy that is merely
    present still receives a small weight. The difference is not a score
    of experimental success.
    """
    return max(float(mean_elo) - 1000.0, 1.0)


def strategy_weights(mean_elo: dict[str, float]) -> dict[str, float]:
    return {name: scheduling_weight(value) for name, value in mean_elo.items()}


def _unit(seed: str) -> float:
    digest = hashlib.sha256(seed.encode()).digest()
    return (int.from_bytes(digest[:8], "big") + 1) / 2**64


def weighted_order(configured: list[str], weights: dict[str, float], seed: str) -> list[str]:
    """Sample without replacement. The same seed and weights always agree.

    Unmeasured names stay in their configured order after the measured ones.
    """
    measured = [name for name in configured if weights.get(name, 0) > 0]
    if len(measured) < 2:
        return list(configured)
    original = {name: index for index, name in enumerate(configured)}

    def key(name: str):
        weight = float(weights.get(name, 0) or 0)
        if weight <= 0:
            return (1, original[name], name)
        score = _unit(f"{seed}:{name}") ** (1.0 / weight)
        return (0, -score, original[name])

    return sorted(configured, key=key)


def sample_choice(options: list[str], weights: dict[str, float], seed: str) -> str:
    if not options:
        raise ValueError("sample_choice requires an option")
    return weighted_order(list(options), weights, seed)[0]


async def mean_elo_by_strategy(conn, session_id: str) -> dict[str, float]:
    async with conn.execute(
        """SELECT strategy, AVG(elo) AS mean_elo FROM hypotheses
            WHERE session_id=? AND matches_played>0 AND elo IS NOT NULL
              AND state NOT IN ('rejected', 'quarantined')
            GROUP BY strategy""",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
    return {row["strategy"]: float(row["mean_elo"]) for row in rows}


async def origin_weights(conn, session_id: str) -> dict[str, float]:
    """Effectiveness of new ideas versus refinements, from matched hypotheses only."""
    async with conn.execute(
        """SELECT created_by, AVG(elo) AS mean_elo FROM hypotheses
            WHERE session_id=? AND matches_played>0 AND elo IS NOT NULL
              AND state NOT IN ('rejected', 'quarantined')
              AND created_by IN ('generation', 'evolution')
            GROUP BY created_by""",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
    return {row["created_by"]: scheduling_weight(row["mean_elo"]) for row in rows}
