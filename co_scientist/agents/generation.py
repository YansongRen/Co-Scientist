"""Generation agent — proposes new hypotheses.

M3 ships the `literature` strategy. `debate` / `assumption` / `feedback_driven`
hook into the same machinery and land in M5+.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import numpy as np

from .. import ids
from ..llm.prompts import render
from ..llm.routing import route
from ..llm.tool_loop import ToolLoopExhausted, run_tool_loop
from ..llm.types import AgentCallSpec, CachedBlock, CallContext
from ..logging import get_logger
from ..models import CitedPaper, Hypothesis, ResearchPlan, Task, TaskResult
from ..safety.evidence import NOVELTY_POLICY, annotate_record, audit_summary, infer_molecule
from ..safety.quoting import quote_untrusted
from ..science.alphaprotein_novo import AlphaProteinNovoAdapter
from ..storage.artifacts import entity_lock, read_json, write_json
from ..storage.repos import embeddings as emb_repo
from ..storage.repos import feedback as fb_repo
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import sessions as sess_repo
from ..vectors.embedder import make_embedder
from ..vectors.store import FaissStore
from .base import AgentDeps, BaseAgent
from .schemas import RECORD_HYPOTHESIS_TOOL

log = get_logger("generation")


class GenerationAgent(BaseAgent):
    name = "generation"

    async def execute(self, task: Task) -> TaskResult:
        strategy = task.payload.get("strategy", "literature")
        n_target = int(task.payload.get("n", 3))

        session = await sess_repo.fetch(self.deps.db, task.session_id)
        if session is None:
            raise RuntimeError(f"session {task.session_id} missing")
        plan = session.research_plan

        if strategy != "literature":
            # M3 ships only the literature strategy.
            raise NotImplementedError(f"strategy {strategy!r} lands in a later milestone")

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
            "generation.literature",
            goal=plan.objective,
            preferences="; ".join(plan.preferences),
            articles_with_reasoning=articles_block,
            instructions=(
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
        _ = n_target  # n_target controls how many parallel Generation tasks are enqueued, not per-call output

        sys_blocks = [
            CachedBlock(self._system_prompt_header(), cache=True),
            CachedBlock(
                _build_session_context(session.research_goal, plan,
                                       await _latest_system_feedback(self.deps, session.id)),
                cache=True,
            ),
        ]
        user_blocks = [CachedBlock(prompt, cache=False)]

        r = route(self.deps.cfg, "generation", "literature")
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
            agent="generation", action="CreateInitialHypotheses", mode="literature",
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
        record = self._final_tool_use(loop_result.response, "record_hypothesis")
        if record is None:
            raise RuntimeError("Generation did not call record_hypothesis")

        # 3. Validate every citation URL is in the union of URLs seen during the loop.
        record["citations"] = _filter_to_seen_urls(record.get("citations", []), loop_result.seen_urls)

        annotate_record(record, loop_result, molecule=plan.target_molecule if plan.target_molecule != "unknown" else infer_molecule(session.research_goal))
        record["parent_ids"] = []  # Generation has no inherited hypotheses.

        # 4. Persist + embed + dedup-check.
        hid, was_new = await self._persist(session.id, record, strategy="literature")
        exact = hid == ids.hypothesis_id(session.id, "generation/literature", record.get("statement") or record.get("title") or "")
        if not was_new and exact:
            existing = await hyp_repo.fetch(self.deps.db, hid)
            record = (await read_json(self.deps.cfg, existing.artifact_path))["record"]
        request = _valid_protein_design_request(self.deps, record.get("protein_design_request"))
        if request is not None and exact:
            request["hypothesis_id"] = hid
        return TaskResult(
            kind="hypothesis_created",
            hypothesis_ids=[hid] if was_new or exact else [],
            extra={"tool_calls": loop_result.tool_calls, "iterations": loop_result.iterations,
                   "protein_design_request": request,
                   "protein_design_requests": [request] if request else []},
        )

    # ---------------------------------------------------------------- #

    async def _persist(
        self, session_id: str, record: dict[str, Any], *, strategy: str
    ) -> tuple[str, bool]:
        statement = record.get("statement") or record.get("title") or ""
        hid = ids.hypothesis_id(session_id, f"generation/{strategy}", statement)
        async with entity_lock(self.deps.cfg, session_id, hid):
            if await hyp_repo.fetch(self.deps.db, hid):
                return hid, False
            return await self._persist_new(session_id, record, strategy=strategy)

    async def _persist_new(
        self, session_id: str, record: dict[str, Any], *, strategy: str
    ) -> tuple[str, bool]:
        statement = record.get("statement") or record.get("title") or ""
        if not statement:
            raise ValueError("record_hypothesis: missing statement")

        origin = f"generation/{strategy}"
        hid = ids.hypothesis_id(session_id, origin, statement)
        summary = (record.get("statement") or "") + "\n\n" + (record.get("mechanism") or "")
        full_text = _render_hypothesis_md(record)

        # Write the JSON artifact first so the row points at a real file.
        artifact_path = await write_json(
            self.deps.cfg, session_id, "hypotheses", hid,
            {"strategy": strategy, "record": record},
        )

        citations = [
            CitedPaper(
                title=c.get("title", ""),
                url=c.get("url", ""),
                excerpt=c.get("excerpt"),
                doi=c.get("doi"),
                year=c.get("year"),
            )
            for c in record.get("citations", [])
            if isinstance(c, dict) and c.get("url")
        ]

        # Step 1: embed + near-neighbour check (does NOT mutate FAISS).
        try:
            dup_id, embed_payload = await self._dedup_query(session_id, summary)
        except Exception as e:
            log.warning("dedup_query_failed", err=str(e))
            dup_id, embed_payload = None, None

        if dup_id is not None and dup_id != hid:
            # Found a near-duplicate already in this session: skip insert + skip FAISS.
            return dup_id, False

        # Step 2: insert the hypothesis row. Deterministic IDs make this idempotent.
        h = Hypothesis(
            id=hid,
            session_id=session_id,
            created_at=datetime.now(UTC),
            created_by="generation",
            strategy=strategy,        # type: ignore[arg-type]
            parent_ids=record.get("parent_ids") or [],
            title=record.get("title", "")[:300],
            summary=(record.get("statement") or "")[:1000],
            full_text=full_text,
            citations=citations,
            artifact_path=artifact_path,
            state="draft",
        )
        inserted = await hyp_repo.insert(self.deps.db, h)

        # Step 3: only add to FAISS if we actually inserted a new row, so FAISS and
        # the hypotheses table can never disagree (FK in embeddings_meta enforces it).
        if inserted and embed_payload is not None:
            try:
                await self._dedup_commit(session_id, hid, embed_payload)
            except Exception as e:
                log.warning("dedup_commit_failed", hypothesis_id=hid, err=str(e))

        return hid, inserted

    async def _dedup_query(
        self, session_id: str, text: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Read-only: embed + nearest-neighbour search. No FAISS mutation.

        Returns (existing_duplicate_id_or_None, embed_payload_for_later_commit).
        """
        try:
            embedder = make_embedder(self.deps.cfg)
        except (RuntimeError, ValueError):
            return None, None
        vec = await embedder.embed([text])
        if vec.size == 0:
            return None, None
        v = vec[0]
        store = FaissStore(self.deps.cfg, session_id, dim=embedder.dim)
        await store.load_or_create()
        nearest = await store.search(np.asarray(v), k=1)
        thr = self.deps.cfg.vectors.dedup_cosine_threshold
        if nearest and nearest[0][1] >= thr:
            return nearest[0][0], None
        payload = {
            "vector": np.asarray(v),
            "model": embedder.model,
            "dim": embedder.dim,
            "text_hash": ids.text_hash(text),
        }
        return None, payload

    async def _dedup_commit(
        self, session_id: str, hypothesis_id: str, payload: dict[str, Any]
    ) -> None:
        """Write-side of dedup: add to FAISS + register the embedding."""
        store = FaissStore(self.deps.cfg, session_id, dim=payload["dim"])
        await store.load_or_create()
        offset = await store.add(hypothesis_id, payload["vector"])
        await store.save()
        await emb_repo.upsert(
            self.deps.db,
            id_=ids.embedding_id(hypothesis_id, payload["model"]),
            session_id=session_id,
            hypothesis_id=hypothesis_id,
            model=payload["model"],
            dim=payload["dim"],
            faiss_offset=offset,
            text_hash=payload["text_hash"],
        )


# --------------------------------------------------------------------------- #
# helpers


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
