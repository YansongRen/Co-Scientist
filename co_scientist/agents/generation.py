"""Generation agent — proposes new hypotheses.

Four explicit bounded prompt strategies; each task registers one hypothesis.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .. import ids
from ..llm.prompts import render
from ..llm.routing import route
from ..llm.tool_loop import ToolLoopExhausted, run_tool_loop
from ..llm.types import AgentCallSpec, CachedBlock, CallContext
from ..logging import get_logger
from ..models import CitedPaper, ResearchPlan, Task, TaskResult
from ..safety.evidence import NOVELTY_POLICY, annotate_record, audit_summary, infer_molecule
from ..safety.quoting import quote_untrusted
from ..science.alphaprotein_novo import AlphaProteinNovoAdapter
from ..storage.artifacts import read_json
from ..storage.repos import feedback as fb_repo
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import sessions as sess_repo
from ..vectors import admission
from ..vectors.embedder import make_embedder
from .base import AgentDeps, BaseAgent
from .generation_strategies import DIVERSITY, STRATEGIES, pool_context, resolve_strategy
from .schemas import RECORD_HYPOTHESIS_TOOL

log = get_logger("generation")


class GenerationAgent(BaseAgent):
    name = "generation"

    async def execute(self, task: Task) -> TaskResult:
        requested_strategy = task.payload.get("strategy", "literature")
        if task.payload.get("n", 1) != 1:
            raise ValueError("Each Generation task must request exactly one hypothesis")
        feedback = await _latest_system_feedback(self.deps, task.session_id)
        feedback = feedback[:self.deps.cfg.generation.feedback_chars] if feedback else None
        strategy, fallback = resolve_strategy(requested_strategy, feedback)
        if fallback:
            log.warning("generation_strategy_fallback", requested=requested_strategy, effective=strategy)
            await admission.note(self.deps, task.session_id, "generation_strategy_fallback",
                requested=requested_strategy, effective=strategy, reason=fallback)

        session = await sess_repo.fetch(self.deps.db, task.session_id)
        if session is None:
            raise RuntimeError(f"session {task.session_id} missing")
        plan = session.research_plan

        # 1. Render the prompt and run the tool loop with `record_hypothesis` available.
        articles_block = (
            "You will gather literature using the available tools (web_search, "
            "pubmed_search, arxiv_search, europe_pmc_search, web_fetch). Pull "
            "abstracts for the most relevant items, then synthesize. After you "
            "have surveyed the literature, call `record_hypothesis` exactly once "
            "with your proposed hypothesis.\n\n"
            + NOVELTY_POLICY
        )

        prompt = render(
            STRATEGIES[strategy].template,
            goal=plan.objective,
            preferences="; ".join(plan.preferences),
            articles_with_reasoning=articles_block,
            instructions=(STRATEGIES[strategy].instruction + "\n" + DIVERSITY + "\n" + (fallback or "") + "\n" +
                "Propose ONE hypothesis (the strongest you can justify) and "
                "register it via the record_hypothesis tool. For each citation, label its "
                "relation, support_level and study scope; include a verbatim excerpt "
                "only when it was actually returned by a tool. Do not propose more "
                "than one — additional hypotheses come from separate Generation calls. "
                "You MUST end this task by calling record_hypothesis; do not keep "
                "searching indefinitely. Budget your literature search to a handful "
                "of queries, then commit."
            ),
        )
        prompt += "\n\n# Existing pool (untrusted summaries)\n" + await pool_context(self.deps, session.id)
        if feedback:
            prompt += "\n\n# Available feedback\n" + quote_untrusted(feedback, id_="generation-feedback")

        sys_blocks = [
            CachedBlock(self._system_prompt_header(), cache=True),
            CachedBlock(
                _build_session_context(session.research_goal, plan,
                                       feedback),
                cache=True,
            ),
        ]
        user_blocks = [CachedBlock(prompt, cache=False)]

        r = route(self.deps.cfg, "generation", strategy)
        tools = [*self.deps.tools.anthropic_tools_for("generation"), RECORD_HYPOTHESIS_TOOL]

        spec = AgentCallSpec(
            route=r,
            system_blocks=sys_blocks,
            user_blocks=user_blocks,
            tools=tools,
            tool_choice={"type": "auto"},
            # A full record_hypothesis payload (statement + mechanism + entities
            # + outcomes + novelty + citations) is large; verbose / reasoning
            # models overran the old 4096 cap mid-JSON, so the arguments string
            # was truncated and unparseable. 8192 leaves room to complete it.
            max_output_tokens=8192,
        )
        ctx = CallContext(
            session_id=task.session_id, task_id=task.id,
            agent="generation", action=task.action, mode=strategy,
        )

        try:
            loop_result = await run_tool_loop(
                self.deps.llm,
                spec=spec, ctx=ctx,
                registry=self.deps.tools,
                max_iters=self.deps.cfg.tool_loop.generation_max_iters,
                parallel_cap=self.deps.cfg.tool_loop.parallel_cap,
                tool_timeout_s=self.deps.cfg.tool_loop.tool_timeout_seconds,
                force_terminal_tool="record_hypothesis",
            )
        except ToolLoopExhausted as e:
            raise RuntimeError(f"generation exhausted tool loop: {e}") from e

        # 2. Extract record_hypothesis from the final assistant message.
        records = [b for b in loop_result.response.raw.content or []
                   if getattr(b, "type", None) == "tool_use" and getattr(b, "name", "") == "record_hypothesis"]
        captured_count = sum(c.get("name") == "record_hypothesis" for c in loop_result.tool_calls)
        if len(records) > 1 or captured_count > 1:
            raise ValueError("Generation permits only one record_hypothesis per task")
        record = self._final_tool_use(loop_result.response, "record_hypothesis")
        if record is None:
            raise RuntimeError("Generation did not call record_hypothesis")

        # 3. Validate every citation URL is in the union of URLs seen during the loop.
        record["citations"] = _filter_to_seen_urls(record.get("citations", []), loop_result.seen_urls)

        annotate_record(record, loop_result, molecule=plan.target_molecule if plan.target_molecule != "unknown" else infer_molecule(session.research_goal))
        record["parent_ids"] = []  # Generation has no inherited hypotheses.

        # 4. Persist + embed + dedup-check.
        hid, was_new = await self._persist(session.id, record, strategy=strategy)
        exact = hid == ids.hypothesis_id(session.id, f"generation/{strategy}", record.get("statement") or record.get("title") or "")
        if not was_new and exact:
            existing = await hyp_repo.fetch(self.deps.db, hid)
            record = (await read_json(self.deps.cfg, existing.artifact_path))["record"]
        request = _valid_protein_design_request(self.deps, record.get("protein_design_request")) if exact else None
        if request is not None and exact:
            request["hypothesis_id"] = hid
        return TaskResult(
            kind="hypothesis_created",
            hypothesis_ids=[hid] if was_new or exact else [],
            extra={"requested_strategy": requested_strategy, "effective_strategy": strategy, "fallback_reason": fallback,
                   "tool_calls": loop_result.tool_calls, "iterations": loop_result.iterations,
                   "protein_design_request": request,
                   "protein_design_requests": [request] if request else []},
        )

    # ---------------------------------------------------------------- #

    async def _persist(self, session_id: str, record: dict[str, Any], *, strategy: str) -> tuple[str | None, bool]:
        citations = [CitedPaper(title=c.get("title", ""), url=c["url"], excerpt=c.get("excerpt"),
            doi=c.get("doi"), year=c.get("year")) for c in record.get("citations", [])
            if isinstance(c, dict) and c.get("url")]
        return await admission.publish(self, session_id, record, strategy, _render_hypothesis_md, citations)

    async def _dedup_query(self, session_id, text):
        return await admission.query(self.deps, session_id, text, make_embedder)

    async def _dedup_commit(self, session_id, hypothesis_id, payload):
        await admission.commit(self.deps, session_id, hypothesis_id, payload)



def _filter_to_seen_urls(
    citations: list[dict[str, Any]], seen: Iterable[str]
) -> list[dict[str, Any]]:
    seen_set = set(seen)
    return [c for c in citations if isinstance(c, dict) and c.get("url") in seen_set]


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
        parts.append("**新颖性状态: 尚未核验。** 检索无结果不代表文献不存在。")
    if record.get("novelty_argument"):
        parts.append(f"## Novelty\n{record['novelty_argument']}")
    if record.get("audit_version"):
        parts.append(audit_summary(record))
    if record.get("citations"):
        parts.append("## Citations")
        for c in record["citations"]:
            year = f" ({c.get('year')})" if c.get("year") else ""
            parts.append(f"- {c.get('title','(no title)')}{year} — {c.get('url','')}")
    return "\n\n".join(parts)


def _build_session_context(goal: str, plan: ResearchPlan, sys_feedback_text: str | None) -> str:
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


def _valid_protein_design_request(deps: AgentDeps, value: Any) -> dict[str, Any] | None:
    """Accept only an explicit, structurally complete request from the model."""
    if not isinstance(value, dict):
        return None
    manifest, output_dir, mode = value.get("manifest_path"), value.get("output_dir"), value.get("mode")
    if not all(isinstance(item, str) and item.strip() for item in (manifest, output_dir, mode)):
        return None
    if mode not in {"validate", "mock", "real"}:
        return None
    try:
        request = AlphaProteinNovoAdapter(deps.cfg).validate_design_request(value)
    except (OSError, ValueError, TypeError):
        return None
    return request.model_dump(mode="json")


async def _latest_system_feedback(deps: AgentDeps, session_id: str) -> str | None:
    fb = await fb_repo.latest_system_feedback(deps.db, session_id)
    return fb.text if fb is not None else None
