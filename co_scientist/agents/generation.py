"""Generation agent — proposes new hypotheses.

Four explicit prompt strategies. Debate and assumption run a bounded series of
text turns, then register one hypothesis. Literature and feedback stay one call.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .. import ids
from ..llm.prompts import render
from ..llm.routing import generation_author, route
from ..llm.tool_loop import ToolLoopExhausted, run_tool_loop
from ..llm.types import AgentCallSpec, CachedBlock, CallContext
from ..logging import get_logger
from ..models import CitedPaper, ResearchPlan, Task, TaskResult
from ..safety.evidence import NOVELTY_POLICY, annotate_record, audit_summary
from ..safety.quoting import quote_untrusted
from ..storage.artifacts import read_json
from ..storage.repos import feedback as fb_repo
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import sessions as sess_repo
from ..vectors import admission
from ..vectors.embedder import make_embedder
from .base import AgentDeps, BaseAgent
from .dialogue import text_turns
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
            "pubmed_search, arxiv_search, europe_pmc_search, web_fetch, read_paper). "
            "Search results are titles and abstracts. Before citing a paper, call read_paper "
            "and use the methods, results, and discussion it returns. If reading_depth is not "
            "full_text, do not describe methods or results you did not receive. "
            "After you have read the papers you will cite, call `record_hypothesis` exactly once "
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
        turns = await self._simulated_turns(task, plan, strategy, sys_blocks)
        if turns:
            prompt += "\n\n# Completed simulated turns\n" + "\n\n".join(
                f"## Turn {number}\n{quote_untrusted(text, id_=f'{strategy}-turn-{number}')}"
                for number, text in enumerate(turns, start=1)
            )
            prompt += "\nRegister exactly one hypothesis that accounts for these turns."
        user_blocks = [CachedBlock(prompt, cache=False)]

        author = generation_author(
            self.deps.cfg, task_id=task.id, batch_index=task.payload.get("batch_index"),
        )
        r = route(self.deps.cfg, "generation", strategy, endpoint=author)
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

        annotate_record(record, loop_result)
        record["parent_ids"] = []  # Generation has no inherited hypotheses.

        # 4. Persist + embed + dedup-check.
        hid, was_new = await self._persist(session.id, record, strategy=strategy)
        exact = hid == ids.hypothesis_id(session.id, f"generation/{strategy}", record.get("statement") or record.get("title") or "")
        if not was_new and exact:
            existing = await hyp_repo.fetch(self.deps.db, hid)
            record = (await read_json(self.deps.cfg, existing.artifact_path))["record"]
        return TaskResult(
            kind="hypothesis_created",
            hypothesis_ids=[hid] if was_new or exact else [],
            extra={"requested_strategy": requested_strategy, "effective_strategy": strategy, "fallback_reason": fallback,
                   "simulated_turns": len(turns),
                   "tool_calls": loop_result.tool_calls, "iterations": loop_result.iterations},
        )

    # ---------------------------------------------------------------- #

    async def _simulated_turns(self, task, plan, strategy: str, system_blocks) -> list[str]:
        if strategy == "debate":
            count = self.deps.cfg.generation.debate_turns - 1
            roles = [
                "Proposer: offer one falsifiable mechanism for the goal. Do not register a hypothesis.",
                "Skeptic: name the load-bearing claim most likely to fail. Do not register a hypothesis.",
                "Experimentalist: name one observation that would separate the two positions. Do not register a hypothesis.",
            ]
        elif strategy == "assumption":
            count = self.deps.cfg.generation.assumption_hops - 1
            roles = [
                "Name one load-bearing assumption and the subassumption it depends on. Do not register a hypothesis.",
                "If that subassumption is true, name the observation that distinguishes it from the original assumption. Do not register a hypothesis.",
                "State whether the subassumption is fundamental to the idea. Do not register a hypothesis.",
            ]
        else:
            return []
        count = max(0, min(count, len(roles)))
        if count == 0:
            return []
        prompts = [
            f"Goal: {plan.objective}\n\nSimulated turn {index} of {count}. {roles[index - 1]}"
            for index in range(1, count + 1)
        ]
        author = generation_author(
            self.deps.cfg, task_id=task.id, batch_index=task.payload.get("batch_index"),
        )
        if author:
            routes = [
                route(self.deps.cfg, "generation", strategy, endpoint=author)
                for _ in range(count)
            ]
        elif strategy == "debate":
            routes = [
                route(self.deps.cfg, "generation", "debate" if index != 2 else "skeptic")
                for index in range(1, count + 1)
            ]
        else:
            routes = None
        return await text_turns(
            self, agent_name="generation", mode=strategy, action=task.action,
            session_id=task.session_id, task_id=task.id, prompts=prompts,
            system_blocks=system_blocks, routes=routes,
        )

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


async def _latest_system_feedback(deps: AgentDeps, session_id: str) -> str | None:
    fb = await fb_repo.latest_system_feedback(deps.db, session_id)
    return fb.text if fb is not None else None
