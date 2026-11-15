"""Reflection agent — reviews a hypothesis from several angles.

The pre-tournament chain is initial (no search) → full literature review →
deep verification → observation → failure-mode simulation. Simulation is
stored as reasoning about possible failure. It is not experimental evidence
and is excluded from ranking and evolution inputs.
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
from ..safety.evidence import annotate_record, audit_summary
from ..safety.quoting import quote_hypothesis, quote_untrusted, short_hash
from ..storage.artifacts import write_json
from ..storage.repos import feedback as fb_repo
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import reviews as rev_repo
from ..storage.repos import scientific_evidence as scientific_evidence_repo
from ..storage.repos import sessions as sess_repo
from .base import BaseAgent
from .schemas import RECORD_REVIEW_TOOL

SEARCH_KINDS = {"full", "verification", "observation", "recurrent"}
NO_SEARCH_KINDS = {"initial", "simulation"}
REVIEW_KINDS = SEARCH_KINDS | NO_SEARCH_KINDS


class ReflectionAgent(BaseAgent):
    name = "reflection"

    async def execute(self, task: Task) -> TaskResult:
        kind = task.payload.get("kind", "full")
        if kind not in REVIEW_KINDS:
            raise ValueError(f"unsupported reflection kind {kind!r}")
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

        scientific_task_id = task.payload.get("scientific_task_id")
        review_id = ids.review_id(h.id, kind, iteration=scientific_task_id or 0)
        previous = await rev_repo.fetch(self.deps.db, review_id)
        if previous is not None:
            return TaskResult(kind="review_completed", hypothesis_ids=[h.id], review_ids=[review_id],
                              extra={"verdict": previous.verdict, "review_kind": kind,
                                     "scientific_task_id": scientific_task_id})

        if kind == "observation":
            article = _observation_article(h)
            if article is None:
                record = {
                    "verdict": "neutral", "kind": "observation", "evidence": [],
                    "notes": ("No retrieved article was available. The observation review "
                              "completed without findings. Absence of an article does not confirm novelty."),
                }
                return await self._store(session, h, task, record, review_id, loop_result=None)

        feedback = await fb_repo.latest_system_feedback(self.deps.db, session.id)
        feedback_text = feedback.text if feedback is not None else ""
        record, loop_result = await self._model_review(session, h, task, kind, feedback_text)
        record["kind"] = kind
        return await self._store(session, h, task, record, review_id, loop_result=loop_result)

    async def _model_review(self, session, h, task, kind, feedback_text):
        evidence_block = await scientific_evidence_repo.render_for_agents(
            self.deps.db, [h.id], include_mock=True, include_invalid=True,
        )
        scientific_evidence = await scientific_evidence_repo.list_for_hypothesis(self.deps.db, h.id)
        article = _observation_article(h) if kind == "observation" else None
        template = "reflection.full" if kind == "recurrent" else f"reflection.{kind}"
        variables = {
            "goal": session.research_plan.objective,
            "preferences": "; ".join(session.research_plan.preferences),
            "hypothesis_id": h.id,
            "hypothesis_text": quote_hypothesis(h.full_text, id_=h.id),
            "hypothesis": quote_hypothesis(h.full_text, id_=h.id),
            "articles_block": (
                "Use web_search, pubmed_search, arxiv_search, europe_pmc_search to find papers, "
                "then read_paper on each paper you rely on. Abstracts only choose what to open. "
                "Cite URLs that appeared in a tool result. If reading_depth is not full_text, "
                "say so and do not describe unread methods or results. "
                "For every evidence item, label relation, support_level and source scope. "
                "Do not equate a matching quotation with support for the hypothesis."
            ),
            "scientific_evidence": evidence_block,
            "article": article["text"] if article else "",
            "article_id": article["id"] if article else "",
            "article_hash": article["hash"] if article else "",
        }
        prompt = render(template, **variables)
        if feedback_text and kind != "initial":
            prompt += (
                "\n\n# Recurring review and tournament notes\n"
                + quote_untrusted(feedback_text[:4000], id_="reflection-feedback")
                + "\nTreat these notes as prior critique. They do not replace retrieved literature "
                "and they are not experimental results."
            )
        if kind == "simulation":
            prompt += "\n\nDo not report this walk-through as data. Do not include citations."

        search = kind in SEARCH_KINDS
        tools = [*self.deps.tools.anthropic_tools_for("reflection"), RECORD_REVIEW_TOOL] if search else [RECORD_REVIEW_TOOL]
        spec = AgentCallSpec(
            route=route(self.deps.cfg, "reflection", kind if kind != "recurrent" else "full"),
            system_blocks=[
                CachedBlock(self._system_prompt_header(), cache=True),
                CachedBlock(
                    f"# Research goal\n{session.research_goal}\n\n"
                    f"# Preferences\n{'; '.join(session.research_plan.preferences)}",
                    cache=True,
                ),
            ],
            user_blocks=[CachedBlock(prompt, cache=False)],
            tools=tools,
            tool_choice={"type": "auto"} if search else {"type": "tool", "name": "record_review"},
            max_output_tokens=4096,
        )
        ctx = CallContext(
            session_id=task.session_id, task_id=task.id,
            agent="reflection", action="ReviewHypothesis", mode=kind,
        )
        try:
            loop_result = await run_tool_loop(
                self.deps.llm, spec=spec, ctx=ctx, registry=self.deps.tools,
                max_iters=1 if not search else self.deps.cfg.tool_loop.reflection_max_iters,
                parallel_cap=self.deps.cfg.tool_loop.parallel_cap,
                tool_timeout_s=self.deps.cfg.tool_loop.tool_timeout_seconds,
                force_terminal_tool="record_review",
            )
        except ToolLoopExhausted as e:
            raise RuntimeError(f"reflection exhausted tool loop: {e}") from e
        record = self._final_tool_use(loop_result.response, "record_review")
        if record is None:
            raise RuntimeError("Reflection did not call record_review")
        if search:
            seen = set(loop_result.seen_urls)
            if article and article.get("url"):
                seen.add(article["url"])
            record["evidence"] = [
                e for e in record.get("evidence", [])
                if isinstance(e, dict) and e.get("url") in seen
            ]
        else:
            record["evidence"] = []
        annotate_record(record, loop_result, review=True)
        if scientific_evidence and kind in {"full", "verification", "recurrent"}:
            record["scientific_evidence_ids"] = [row.id for row in scientific_evidence]
            record["scientific_evidence_status"] = sorted({row.status for row in scientific_evidence})
        return record, loop_result

    async def _store(self, session, h, task, record, review_id, *, loop_result) -> TaskResult:
        kind = record.get("kind") or task.payload.get("kind", "full")
        record["kind"] = kind
        if loop_result is not None and "evidence_audit" not in record:
            annotate_record(record, loop_result, review=True)
        scores = ReviewScores(**{key: record.get(key) for key in
                                 ("novelty", "correctness", "testability", "feasibility")})
        artifact_path = await write_json(
            self.deps.cfg, session.id, "reviews", review_id,
            {"hypothesis_id": h.id, "record": record},
        )
        review = Review(
            id=review_id, hypothesis_id=h.id, session_id=session.id,
            created_at=datetime.now(UTC), kind=kind,
            verdict=record.get("verdict"), scores=scores,
            body=_render_review_md(record), artifact_path=artifact_path,
        )
        await rev_repo.insert(self.deps.db, review)
        if kind == "full":
            await hyp_repo.set_state_if(
                self.deps.db, h.id, new_state="reviewed", expected_states=("draft",),
            )
        return TaskResult(
            kind="review_completed", review_ids=[review_id], hypothesis_ids=[h.id],
            extra={"verdict": record.get("verdict"), "review_kind": kind,
                   "scientific_task_id": task.payload.get("scientific_task_id")},
        )


def _observation_article(h) -> dict[str, str] | None:
    for citation in h.citations:
        excerpt = (citation.excerpt or "").strip()
        if citation.url and len(excerpt) >= 40:
            return {
                "url": citation.url,
                "text": quote_untrusted(excerpt, id_=short_hash(citation.url)),
                "id": short_hash(citation.url),
                "hash": short_hash(excerpt),
            }
    return None


def _render_review_md(record: dict[str, Any]) -> str:
    parts: list[str] = ["# Review"]
    if record.get("kind") == "simulation":
        parts.append("**模拟审查:** 下面是逐步推演可能的失败点，不是实验结果，也不进入 Elo 排名依据。")
    if record.get("kind") == "initial":
        parts.append("**初审:** 未检索文献。通过只表示没有明显内部矛盾，不表示假设成立。")
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
