"""Evolution agent — combines, simplifies, grounds, and reimagines top hypotheses.

Strategies:
- `combine`     — merge two distant top hypotheses into a stronger one.
- `simplify`    — strip a hypothesis to its load-bearing claim.
- `feasibility` — make it implementable with current tech.
- `out_of_box`  — out-of-box synthesis inspired by top-K.
- `grounding`   — search the literature and fill one reasoning gap in a new child.

Each produces a *new* hypothesis row with `parent_ids` populated, which then
cascades into Reflection → Ranking like any fresh idea.
"""

from __future__ import annotations

from typing import Any, Literal

from .. import ids
from ..llm.prompts import render
from ..llm.routing import route
from ..llm.tool_loop import ToolLoopExhausted, run_tool_loop
from ..llm.types import AgentCallSpec, CachedBlock, CallContext
from ..logging import get_logger
from ..models import CitedPaper, Hypothesis, Task, TaskResult
from ..safety.evidence import NOVELTY_POLICY, annotate_record, audit_summary
from ..safety.quoting import quote_hypothesis
from ..storage.repos import feedback as fb_repo
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import reviews as rev_repo
from ..storage.repos import scientific_evidence as scientific_evidence_repo
from ..storage.repos import sessions as sess_repo
from ..vectors import admission
from ..vectors.embedder import make_embedder
from ..vectors.store import FaissStore
from .base import AgentDeps, BaseAgent
from .schemas import RECORD_HYPOTHESIS_TOOL

log = get_logger("evolution")

EvoStrategy = Literal["combine", "simplify", "feasibility", "out_of_box", "grounding"]


class EvolutionAgent(BaseAgent):
    name = "evolution"

    async def execute(self, task: Task) -> TaskResult:
        strategies: list[EvoStrategy] = task.payload.get("strategies") or self.deps.cfg.evolution.strategies
        if any(s not in ("combine", "simplify", "feasibility", "out_of_box", "grounding") for s in strategies):
            raise ValueError("Unsupported Evolution strategy")
        top_k = int(task.payload.get("top_k", 5))

        session = await sess_repo.fetch(self.deps.db, task.session_id)
        if session is None:
            raise RuntimeError(f"session {task.session_id} missing")

        self._frozen_reviews = None
        if "parent_ids" in task.payload:
            parent_ids = task.payload["parent_ids"]
            review_ids = task.payload.get("parent_review_ids", {})
            if not isinstance(parent_ids, list) or len(parent_ids) < 2 or len(set(parent_ids)) != len(parent_ids):
                raise ValueError("Evolution requires distinct parent IDs")
            found = {h.id: h for h in await hyp_repo.fetch_many(self.deps.db, parent_ids)}
            if any(hid not in found or found[hid].session_id != session.id for hid in parent_ids):
                raise ValueError("Evolution parent/session mismatch")
            top = [found[hid] for hid in parent_ids]
            self._frozen_reviews = {}
            for h in top:
                reviews = await rev_repo.reasoning_reviews(self.deps.cfg, self.deps.db,
                    await rev_repo.list_for_hypothesis(self.deps.db, h.id))
                review = next((r for r in reviews if r.id == review_ids.get(h.id)), None)
                if h.state not in ("in_tournament", "pinned") or review is None:
                    return TaskResult(kind="noop", extra={"reason": "parent snapshot no longer admissible"})
                self._frozen_reviews[h.id] = review.body
        else:
            top = await hyp_repo.top_by_elo(self.deps.db, session.id, k=top_k)
        if len(top) < 2:
            return TaskResult(kind="noop", extra={"reason": "need at least 2 top hypotheses"})

        new_ids: list[str] = []
        attempted = []
        for strat in strategies:
            current = await hyp_repo.list_for_session(self.deps.db, session.id)
            if len(current) >= self.deps.cfg.run.max_ideas:
                break
            attempted.append(strat)
            await self._activity(task, "正在根据反馈演化假设", strategy=strat,
                                 hypothesis_ids=[h.id for h in top])
            try:
                hid = await self._evolve_one(session, top, strategy=strat)
            except Exception as e:
                log.warning("evolution_strategy_failed", strategy=strat, err=str(e))
                continue
            if hid:
                new_ids.append(hid)

        return TaskResult(
            kind="hypothesis_created",
            hypothesis_ids=new_ids,
            extra={
                "strategies_used": attempted,
                "parent_ids": [h.id for h in top],
                "parent_review_ids": task.payload.get("parent_review_ids", {}),
            },
        )

    # ----------------------------- one strategy ----------------------------- #

    async def _evolve_one(
        self, session, top: list[Hypothesis], *, strategy: EvoStrategy
    ) -> str | None:
        if strategy == "combine":
            return await self._combine(session, top)
        if strategy == "out_of_box":
            return await self._out_of_box(session, top)
        return await self._unary(session, top, strategy=strategy)

    async def _combine(self, session, top: list[Hypothesis]) -> str | None:
        # Pick the most idea-distant pair within the top set.
        pair = await self._most_distant_pair(session.id, top)
        if pair is None:
            return None
        a, b = pair
        review_a = await self._best_review(a.id)
        review_b = await self._best_review(b.id)
        prompt = render(
            "evolution.combine",
            goal=session.research_plan.objective,
            preferences="; ".join(session.research_plan.preferences),
            hypothesis_a_id=a.id, hypothesis_a=quote_hypothesis(a.full_text, id_=a.id),
            hypothesis_b_id=b.id, hypothesis_b=quote_hypothesis(b.full_text, id_=b.id),
            review_a=review_a, review_b=review_b,
        )
        return await self._run_and_persist(
            session, prompt, strategy="combine",
            mode_for_route="combine", parent_ids=[a.id, b.id],
        )

    async def _out_of_box(self, session, top: list[Hypothesis]) -> str | None:
        inspirations = top[:5]
        prompt = render(
            "evolution.out_of_box",
            goal=session.research_plan.objective,
            preferences="; ".join(session.research_plan.preferences),
            hypotheses=[
                {"id": h.id, "text": quote_hypothesis(h.full_text, id_=h.id)}
                for h in inspirations
            ],
        )
        return await self._run_and_persist(
            session, prompt, strategy="out_of_box",
            mode_for_route="out_of_box",
            parent_ids=[h.id for h in inspirations],
        )

    async def _unary(
        self, session, top: list[Hypothesis], *, strategy: EvoStrategy
    ) -> str | None:
        h = top[0]
        review = await self._best_review(h.id)
        template = f"evolution.{strategy}"
        prompt = render(
            template,
            goal=session.research_plan.objective,
            preferences="; ".join(session.research_plan.preferences),
            hypothesis_id=h.id, hypothesis=quote_hypothesis(h.full_text, id_=h.id),
            review=review,
        )
        return await self._run_and_persist(
            session, prompt, strategy=strategy,
            mode_for_route=strategy,
            parent_ids=[h.id],
        )

    # ----------------------------- run + persist ----------------------------- #

    async def _run_and_persist(
        self,
        session,
        prompt: str,
        *,
        strategy: EvoStrategy,
        mode_for_route: str,
        parent_ids: list[str],
    ) -> str | None:
        sys_blocks = [
            CachedBlock(self._system_prompt_header() + "\n" + NOVELTY_POLICY, cache=True),
            CachedBlock(
                _build_session_context(session.research_goal, session.research_plan,
                                       await self._latest_feedback(session.id)),
                cache=True,
            ),
        ]
        user_blocks = [CachedBlock(prompt, cache=False)]
        evidence_context = await self._scientific_evidence_context(parent_ids)
        if evidence_context:
            user_blocks.append(CachedBlock(evidence_context, cache=False))

        r = route(self.deps.cfg, "evolution", mode_for_route)
        tools = [*self.deps.tools.anthropic_tools_for("evolution"), RECORD_HYPOTHESIS_TOOL]
        spec = AgentCallSpec(
            route=r,
            system_blocks=sys_blocks,
            user_blocks=user_blocks,
            tools=tools,
            tool_choice={"type": "auto"},
            max_output_tokens=4096,
        )
        ctx = CallContext(
            session_id=session.id, task_id=None,
            agent="evolution", action="EvolveTopHypotheses", mode=mode_for_route,
        )
        try:
            result = await run_tool_loop(
                self.deps.llm, spec=spec, ctx=ctx,
                registry=self.deps.tools,
                max_iters=self.deps.cfg.tool_loop.evolution_max_iters,
                parallel_cap=self.deps.cfg.tool_loop.parallel_cap,
                tool_timeout_s=self.deps.cfg.tool_loop.tool_timeout_seconds,
                force_terminal_tool="record_hypothesis",
            )
        except ToolLoopExhausted as e:
            log.warning("evolution_tool_loop_exhausted", err=str(e))
            return None

        record = self._final_tool_use(result.response, "record_hypothesis")
        if record is None:
            log.warning("evolution_no_record")
            return None

        # Citation URL filter (same as Generation).
        record["citations"] = [
            c for c in record.get("citations", [])
            if isinstance(c, dict) and c.get("url") in result.seen_urls
        ]
        annotate_record(record, result)
        record["strategy"] = strategy
        record["parent_ids"] = parent_ids

        hid, was_new = await self._persist(session.id, record, strategy=strategy)
        if not hid:
            return None
        stored = await hyp_repo.fetch(self.deps.db, hid)
        if stored is not None and stored.state == "quarantined":
            return None
        exact = hid == ids.hypothesis_id(session.id, f"evolution/{strategy}", record.get("statement") or record.get("title") or "")
        return hid if was_new or exact else None

    async def _persist(self, session_id: str, record: dict[str, Any], *, strategy: str) -> tuple[str | None, bool]:
        citations = [CitedPaper(title=c.get("title", ""), url=c["url"], excerpt=c.get("excerpt"),
            doi=c.get("doi"), year=c.get("year")) for c in record.get("citations", [])
            if isinstance(c, dict) and c.get("url")]
        return await admission.publish(self, session_id, record, strategy, _render_hypothesis_md, citations)

    async def _dedup_query(self, session_id, text):
        return await admission.query(self.deps, session_id, text, make_embedder)

    async def _dedup_commit(self, session_id, hypothesis_id, payload):
        await admission.commit(self.deps, session_id, hypothesis_id, payload)

    async def _most_distant_pair(
        self, session_id: str, top: list[Hypothesis]
    ) -> tuple[Hypothesis, Hypothesis] | None:
        if len(top) < 2:
            return None
        try:
            embedder = make_embedder(self.deps.cfg)
        except (RuntimeError, ValueError):
            return top[0], top[1]
        store = FaissStore(self.deps.cfg, session_id, dim=embedder.dim, model=embedder.model)
        await store.load_or_create()
        if store.n == 0:
            return top[0], top[1]
        best: tuple[Hypothesis, Hypothesis] | None = None
        best_sim = 2.0
        vecs = store.index.reconstruct_n(0, store.n)
        for i, a in enumerate(top):
            ia = store.offset_of(a.id)
            if ia is None:
                continue
            for b in top[i + 1:]:
                ib = store.offset_of(b.id)
                if ib is None:
                    continue
                sim = float(vecs[ia] @ vecs[ib])
                if sim < best_sim:
                    best_sim = sim
                    best = (a, b)
        return best or (top[0], top[1])

    async def _best_review(self, hypothesis_id: str) -> str | None:
        if getattr(self, "_frozen_reviews", None) is not None:
            return self._frozen_reviews.get(hypothesis_id)
        return await rev_repo.best_for_reasoning(self.deps.cfg, self.deps.db, hypothesis_id)

    async def _latest_feedback(self, session_id: str) -> str | None:
        text = await fb_repo.compose_prompt(
            self.deps.db, session_id, limit_chars=self.deps.cfg.generation.feedback_chars,
        )
        return text or None

    async def _scientific_evidence_context(self, hypothesis_ids: list[str]) -> str:
        return await scientific_evidence_repo.render_for_agents(
            self.deps.db, hypothesis_ids, include_mock=False, include_invalid=False,
        )


# ----------------------------- formatting helpers ----------------------------- #


def _render_hypothesis_md(record: dict[str, Any]) -> str:
    parts: list[str] = []
    if record.get("title"):
        parts.append(f"# {record['title']}")
    parts.append(f"**Hypothesis.** {record.get('statement', '')}")
    if record.get("mechanism"):
        parts.append(f"## Mechanism\n{record['mechanism']}")
    if record.get("entities"):
        parts.append("## Entities\n- " + "\n- ".join(record["entities"]))
    if record.get("anticipated_outcomes"):
        parts.append(f"## Anticipated outcomes\n{record['anticipated_outcomes']}")
    if record.get("novelty_assessment"):
        parts.append("**新颖性尚未核验。** 检索无结果不代表文献不存在。")
    if record.get("novelty_argument"):
        parts.append(f"## Novelty\n{record['novelty_argument']}")
    if record.get("audit_version"):
        parts.append(audit_summary(record))
    if record.get("citations"):
        parts.append("## Citations")
        for c in record["citations"]:
            year = f" ({c.get('year')})" if c.get("year") else ""
            parts.append(f"- {c.get('title','(no title)')}{year} — {c.get('url','')}")
    if record.get("parent_ids"):
        parts.append(f"## Parents\n{', '.join(record['parent_ids'])}")
    return "\n\n".join(parts)


def _build_session_context(goal: str, plan, sys_feedback_text: str | None) -> str:
    from ..safety.quoting import quote_untrusted

    fb = ""
    if sys_feedback_text:
        fb = "\n\n# Researcher / Meta-review Feedback\n" + quote_untrusted(
            sys_feedback_text, id_="system_feedback:latest"
        )
    return (
        f"# Research goal\n{goal}\n\n"
        f"# Parsed plan\n"
        f"- Objective: {plan.objective}\n"
        f"- Preferences: {'; '.join(plan.preferences) or '(none)'}\n"
        f"- Idea attributes: {'; '.join(plan.idea_attributes) or '(none)'}\n"
        f"- Constraints: {'; '.join(plan.constraints) or '(none)'}\n"
        f"{fb}"
    )
