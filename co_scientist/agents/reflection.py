"""Reflection agent — reviews a hypothesis.

M3 ships the `full` review mode. `verification` and `observation` reuse the same
machinery in later milestones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .. import ids
from ..llm.prompts import render
from ..llm.routing import route
from ..llm.tool_loop import ToolLoopExhausted, run_tool_loop
from ..llm.types import AgentCallSpec, CachedBlock, CallContext
from ..models import Review, ReviewScores, Task, TaskResult
from ..safety.evidence import annotate_record, audit_summary, infer_molecule
from ..safety.quoting import quote_hypothesis
from ..storage.artifacts import write_json
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import reviews as rev_repo
from ..storage.repos import scientific_evidence as scientific_evidence_repo
from ..storage.repos import sessions as sess_repo
from .base import BaseAgent
from .schemas import RECORD_REVIEW_TOOL


class ReflectionAgent(BaseAgent):
    name = "reflection"

    async def execute(self, task: Task) -> TaskResult:
        kind = task.payload.get("kind", "full")
        hypothesis_id = task.target_id
        if not hypothesis_id:
            raise ValueError("ReflectionAgent.execute requires target_id (hypothesis_id)")

        session = await sess_repo.fetch(self.deps.db, task.session_id)
        if session is None:
            raise RuntimeError(f"session {task.session_id} missing")
        h = await hyp_repo.fetch(self.deps.db, hypothesis_id)
        if h is None:
            raise RuntimeError(f"hypothesis {hypothesis_id} missing")
        if h.session_id != task.session_id:
            raise ValueError("reflection hypothesis/session mismatch")

        # One immutable review per scientific run; retries reuse it.
        scientific_task_id = task.payload.get("scientific_task_id")
        review_id = ids.review_id(h.id, "full", iteration=scientific_task_id or 0)
        previous = await rev_repo.fetch(self.deps.db, review_id)
        if previous is not None:
            return TaskResult(kind="review_completed", hypothesis_ids=[h.id], review_ids=[review_id],
                              extra={"verdict": previous.verdict})

        if kind != "full":
            raise NotImplementedError(f"reflection kind {kind!r} lands in a later milestone")

        scientific_evidence = await scientific_evidence_repo.list_for_hypothesis(
            self.deps.db, h.id
        )
        evidence_block = await scientific_evidence_repo.render_for_agents(
            self.deps.db, [h.id], include_mock=True, include_invalid=True,
        )

        prompt = render(
            "reflection.full",
            goal=session.research_plan.objective,
            preferences="; ".join(session.research_plan.preferences),
            hypothesis_id=h.id,
            hypothesis_text=quote_hypothesis(h.full_text, id_=h.id),
            articles_block=(
                "Use the available search tools (web_search, pubmed_search, "
                "arxiv_search, europe_pmc_search, web_fetch) to gather supporting "
                "and contradicting evidence. Cite URLs that you actually fetched. "
                "For every evidence item, label relation, support_level and source scope. "
                "Distinguish DNA from RNA and label cross-scope extrapolations as uncertain. "
                "Do not equate a matching quotation with support for the hypothesis."
            ),
            scientific_evidence=evidence_block,
        )

        sys_blocks = [
            CachedBlock(self._system_prompt_header(), cache=True),
            CachedBlock(
                f"# Research goal\n{session.research_goal}\n\n"
                f"# Preferences\n{'; '.join(session.research_plan.preferences)}",
                cache=True,
            ),
        ]
        user_blocks = [CachedBlock(prompt, cache=False)]

        r = route(self.deps.cfg, "reflection", "full")
        tools = [*self.deps.tools.anthropic_tools_for("reflection"), RECORD_REVIEW_TOOL]

        spec = AgentCallSpec(
            route=r,
            system_blocks=sys_blocks,
            user_blocks=user_blocks,
            tools=tools,
            tool_choice={"type": "auto"},
            max_output_tokens=4096,
        )
        ctx = CallContext(
            session_id=task.session_id, task_id=task.id,
            agent="reflection", action="ReviewHypothesis", mode="full",
        )

        try:
            loop_result = await run_tool_loop(
                self.deps.llm,
                spec=spec, ctx=ctx,
                registry=self.deps.tools,
                max_iters=self.deps.cfg.tool_loop.reflection_max_iters,
                parallel_cap=self.deps.cfg.tool_loop.parallel_cap,
                tool_timeout_s=self.deps.cfg.tool_loop.tool_timeout_seconds,
                force_terminal_tool="record_review",
            )
        except ToolLoopExhausted as e:
            raise RuntimeError(f"reflection exhausted tool loop: {e}") from e

        record = self._final_tool_use(loop_result.response, "record_review")
        if record is None:
            raise RuntimeError("Reflection did not call record_review")

        # Drop evidence entries whose URL we never saw — keep the review honest.
        seen = loop_result.seen_urls
        record["evidence"] = [
            e for e in record.get("evidence", [])
            if isinstance(e, dict) and e.get("url") in seen
        ]

        annotate_record(record, loop_result, review=True, molecule=session.research_plan.target_molecule if session.research_plan.target_molecule != "unknown" else infer_molecule(session.research_goal))
        if scientific_evidence:
            record["scientific_evidence_ids"] = [row.id for row in scientific_evidence]
            record["scientific_evidence_status"] = sorted({row.status for row in scientific_evidence})

        # Validate model numbers before publishing any artifact.
        scores = ReviewScores(**{key: record.get(key) for key in
                                 ("novelty", "correctness", "testability", "feasibility")})
        artifact_path = await write_json(
            self.deps.cfg, session.id, "reviews", review_id,
            {"hypothesis_id": h.id, "record": record},
        )
        body_md = _render_review_md(record)
        review = Review(
            id=review_id,
            hypothesis_id=h.id,
            session_id=session.id,
            created_at=datetime.now(UTC),
            kind="full",
            verdict=record.get("verdict"),       # type: ignore[arg-type]
            scores=scores,
            body=body_md,
            artifact_path=artifact_path,
        )
        await rev_repo.insert(self.deps.db, review)
        # Only promote draft → reviewed. If Reflection re-fires on an
        # already-ranked/evolved/pinned hypothesis we must not drag it back.
        await hyp_repo.set_state_if(
            self.deps.db, h.id, new_state="reviewed", expected_states=("draft",),
        )

        return TaskResult(
            kind="review_completed",
            review_ids=[review_id],
            hypothesis_ids=[h.id],
            extra={"verdict": record.get("verdict")},
        )


def _render_review_md(record: dict[str, Any]) -> str:
    parts: list[str] = ["# Review"]
    if record.get("evidence_audit"):
        parts.append("**证据核查说明: ** 引文匹配仅核对工具返回内容; 是否支持论断及跨研究对象外推仍需人工核验。")
    if record.get("verdict"):
        parts.append(f"**Verdict.** {record['verdict']}")
    scores = []
    for s in ("novelty", "correctness", "testability", "feasibility"):
        if record.get(s) is not None:
            scores.append(f"{s} {record[s]:.2f}")
    if scores:
        parts.append("**Scores.** " + " · ".join(scores))
    if record.get("audit_version"):
        parts.append(audit_summary(record))
    if record.get("assumptions"):
        parts.append("## Assumptions")
        for a in record["assumptions"]:
            parts.append(
                f"- *{a.get('plausibility','?')}*: {a.get('assumption','')}\n  "
                f"  {a.get('rationale','')}"
            )
    if record.get("evidence"):
        parts.append("## Evidence")
        for e in record["evidence"]:
            parts.append(f"- {e.get('claim','')} — {e.get('url','')}\n  > {e.get('excerpt','')}")
    if record.get("notes"):
        parts.append(f"## Notes\n{record['notes']}")
    return "\n\n".join(parts)


def _scientific_evidence_block(rows) -> str:
    if not rows:
        return "No AlphaProtein Novo evidence is attached to this review."
    lines = [
        "The following values are deterministic outputs parsed from persisted AlphaProtein Novo artifacts.",
        "Use the numbers exactly as shown; interpret them as computational evidence only. Do not infer experimental catalytic activity.",
    ]
    for row in rows:
        lines.append(
            f"- evidence_id={row.id}; design={row.design_id or '?'}; "
            f"kind={row.kind}; status={row.status}; metric={row.metric_name}; "
            f"value={row.metric_value!r}; source={row.source_path}; artifact={row.artifact_path}"
        )
    return "\n".join(lines)
