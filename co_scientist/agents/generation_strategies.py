"""Explicit prompt strategies, not extra agents or unbounded debate loops."""
from dataclasses import dataclass

from ..safety.quoting import quote_untrusted


@dataclass(frozen=True)
class GenerationStrategy:
    template: str
    instruction: str


STRATEGIES = {
    "literature": GenerationStrategy("generation.literature",
        "Survey relevant sources, then call read_paper on each paper you will cite. Use the methods, results, and discussion it returns. If reading_depth is not full_text, say the claim rests on the abstract or that the full text was unavailable."),
    "debate": GenerationStrategy("generation.debate",
        "The simulated proposer, skeptic and experimentalist turns above are already complete. "
        "Reconcile them into the ONE final hypothesis. "
        "These are simulated perspectives, not independent experiments or external expert opinions."),
    "assumption": GenerationStrategy("generation.assumption",
        "The assumption hops above are already complete. Aggregate them into ONE falsifiable hypothesis "
        "and the observation that would distinguish it from the original assumption."),
    "feedback_driven": GenerationStrategy("generation.feedback_driven",
        "Use actual researcher/meta-review feedback to address an identified weakness or unexplored direction. "
        "Explain which feedback motivated the ONE new hypothesis; do not merely rephrase a prior candidate."),
}

DIVERSITY = """Batch diversity requirements:
Do not repeat the core conclusion of an existing hypothesis. Explore different mechanisms,
entities, causal paths or counterexamples. If the pool already covers a direction, choose
another one. Renaming terms is not scientific novelty. Pool coverage is bounded and incomplete;
absence from the excerpt does not prove novelty. Register exactly ONE record_hypothesis.
Do not fabricate references, scientific results, catalytic motifs, ligands or local file paths.
Only register one hypothesis.
"""


def resolve_strategy(name, feedback):
    if not isinstance(name, str) or name not in STRATEGIES:
        raise ValueError(f"Unsupported Generation strategy: {name!r}")
    if name == "feedback_driven" and not (feedback or "").strip():
        return "assumption", "No prior feedback available; explicitly using assumption strategy."
    return name, None


async def pool_context(deps, sid):
    # Fetch bounded summaries directly; never hydrate all full-text artifacts.
    async with deps.db.execute("""SELECT id,summary,strategy,dedup_cluster FROM hypotheses
            WHERE session_id=? ORDER BY created_at DESC,id LIMIT ?""",
            (sid, deps.cfg.generation.context_hypotheses)) as cur:
        rows = await cur.fetchall()
    lines = [f"{r['id']} | strategy={r['strategy']} | cluster={r['dedup_cluster'] or 'unassigned'} | {r['summary'][:500]}"
             for r in rows]
    body = "\n".join(lines) or "(no hypotheses yet)"
    return quote_untrusted(body[:deps.cfg.generation.context_chars], id_="bounded-hypothesis-pool")


def allocate(cfg, count, *, offset=0, names=None):
    names = list(names) if names is not None else list(cfg.generation.strategies)
    if not names:
        raise ValueError("At least one Generation strategy is required")
    for name in names:
        if name not in STRATEGIES:
            raise ValueError(f"Unsupported Generation strategy: {name!r}")
    return [names[(offset+i) % len(names)] for i in range(count)]
