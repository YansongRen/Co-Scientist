"""Supervisor — durable task scheduler for the multi-agent system.

Responsibilities:
1. Parse the scientist's goal into a ResearchPlan.
2. Bootstrap the session (insert row, reclaim expired leases on resume).
3. Run a bounded asyncio worker pool that claims tasks from the DB-backed queue.
4. Apply follow-up scheduling rules after each task completes.
5. Periodically run `decide_next_steps` when the queue is idle:
   - Tournament refinement.
   - Evolution if the leaderboard is stable.
   - Periodic system-feedback meta-reviews.
6. Check the termination predicate after every task; on stop, cancel pending
   work and run a single final meta-review for the overview.
7. Honor pause / abort via DB-flagged session.status.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from .. import ids
from ..config import Config
from ..llm.budgets import TokenBudget
from ..llm.prompts import render
from ..llm.provider import get_provider
from ..llm.routing import route
from ..llm.types import (
    AgentCallSpec,
    CachedBlock,
    CallContext,
)
from ..logging import bind, get_logger
from ..models import ResearchPlan, Session, Task, TaskResult
from ..obs.activity import activity_task, safe_text
from ..orchestrator.events import GLOBAL_BUS
from ..orchestrator.termination import (
    StabilityTracker,
    StopReason,
    should_stop,
    snapshot_top_k,
)
from ..storage import db as db_mod
from ..storage.artifacts import write_text
from ..storage.repos import events as events_repo
from ..storage.repos import feedback as fb_repo
from ..storage.repos import hypotheses as hyp_repo
from ..storage.repos import reviews as rev_repo
from ..storage.repos import sessions as sess_repo
from ..storage.repos import tasks as task_repo
from ..tools.registry import ToolRegistry
from .base import AgentDeps
from .generation import GenerationAgent
from .ranking import RankingAgent
from .reflection import ReflectionAgent
from .schemas import RECORD_RESEARCH_PLAN_TOOL

log = get_logger("supervisor")


# ----------------------------- public API ----------------------------- #


class Supervisor:
    """One-process Supervisor; CLI invokes via `await supervisor.run_session(...)`."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def run_session(
        self,
        goal: str,
        *,
        preferences_text: str | None = None,
        n_initial: int = 3,
        wall_clock_seconds: int | None = None,
        resume_session_id: str | None = None,
        prepared_session_id: str | None = None,
    ) -> str:
        conn = await db_mod.connect(self.cfg)
        llm_conn = None
        try:
            llm_conn = await db_mod.connect(self.cfg)
            if resume_session_id is None:
                if prepared_session_id:
                    session = await sess_repo.fetch(conn, prepared_session_id)
                    if session is None or session.status != "paused":
                        raise RuntimeError("Prepared research session is missing or already started")
                    # An atomic transition prevents two web deliveries starting
                    # the same prepared run. Normal CLI bootstrap is unchanged.
                    cur = await conn.execute("UPDATE sessions SET status='running' WHERE id=? AND status='paused'", (session.id,))
                    await conn.commit()
                    if cur.rowcount != 1:
                        raise RuntimeError("Prepared research session already claimed")
                    goal = session.research_goal
                    preferences_text = session.config_snapshot.get("web_launch", {}).get("preferences")
                else:
                    session = await self._create_session(conn, goal, preferences_text, wall_clock_seconds)
                bind(session_id=session.id)
                log.info(
                    "session_started",
                    goal=goal[:120], session_id=session.id,
                    budget_usd=session.budget_usd, n_initial=n_initial,
                )
                await self._emit(conn, session.id, "session_started", {
                    "goal": goal[:200], "n_initial": n_initial,
                    "budget_usd": session.budget_usd,
                })
                budget = TokenBudget(
                    cfg=self.cfg,
                    budget_tokens=session.budget_tokens,
                    budget_usd=session.budget_usd,
                )
                llm = get_provider(self.cfg, db=llm_conn, budget=budget)
                tools = ToolRegistry(self.cfg).discover()
                deps = AgentDeps(cfg=self.cfg, db=conn, llm=llm, tools=tools)

                confirmed = session.config_snapshot.get("web_launch", {}).get("confirmed_brief")
                if prepared_session_id and preferences_text and not confirmed:
                    await fb_repo.insert(conn, _human_preference(session.id, preferences_text))
                # A human-confirmed structured brief already supplies the plan.
                plan = session.research_plan if confirmed else await self._parse_goal(deps, session, goal, preferences_text)
                await self._apply_plan(conn, session, plan)
                session = await sess_repo.fetch(conn, session.id)
                assert session is not None

                for i in range(n_initial):
                    await task_repo.enqueue(conn, Task(
                        id=ids.task_id(), session_id=session.id,
                        created_at=datetime.now(UTC),
                        agent="generation", action="CreateInitialHypotheses",
                        payload={"strategy": "literature", "n": 1},
                        priority=100, status="pending",
                        idempotency_key=f"{session.id}::generation::initial::{i}",
                    ))
            else:
                session = await sess_repo.fetch(conn, resume_session_id)
                if session is None:
                    raise RuntimeError(f"no such session: {resume_session_id}")
                bind(session_id=session.id)
                log.info("session_resumed", session_id=session.id, status=session.status)
                reclaimed = await task_repo.reclaim_expired_leases(
                    conn, session.id, max_attempts=self.cfg.lease.max_attempts,
                )
                log.info("leases_reclaimed", **reclaimed)
                if session.status != "running":
                    await sess_repo.set_status(conn, session.id, "running")
                budget = TokenBudget(
                    cfg=self.cfg,
                    budget_tokens=session.budget_tokens,
                    budget_usd=session.budget_usd,
                )
                await budget.restore_usage(conn, session)
                llm = get_provider(self.cfg, db=llm_conn, budget=budget)
                tools = ToolRegistry(self.cfg).discover()
                deps = AgentDeps(cfg=self.cfg, db=conn, llm=llm, tools=tools)

            tracker = StabilityTracker(
                k=self.cfg.termination.elo_stability_k,
                n=self.cfg.termination.elo_stability_n,
                eps=self.cfg.termination.elo_stability_eps,
                min_ideas=self.cfg.termination.min_ideas_before_stable,
                min_matches=self.cfg.termination.min_matches_before_stable,
            )

            stop_reason = await self._main_loop(conn, deps, session, tracker)
            log.info("main_loop_exit", stop_reason=stop_reason.value if stop_reason else "none")

            await self._finalize(conn, deps, session, stop_reason)
            return session.id
        finally:
            if llm_conn is not None:
                await llm_conn.close()
            await conn.close()

    # ----------------------------- session bootstrap ----------------------------- #

    async def _create_session(
        self,
        conn: aiosqlite.Connection,
        goal: str,
        preferences_text: str | None,
        wall_clock_seconds: int | None,
    ) -> Session:
        sid = ids.session_id()
        now = datetime.now(UTC)
        wall = wall_clock_seconds or self.cfg.run.wall_clock_seconds
        from datetime import timedelta

        plan = ResearchPlan(objective=goal.strip(), preferences=[], idea_attributes=[])
        snap: dict[str, Any] = json.loads(json.dumps(self.cfg.model_dump(exclude={"secrets"})))
        s = Session(
            id=sid, created_at=now, updated_at=now, status="running",
            research_goal=goal, research_plan=plan,
            config_snapshot=snap,
            budget_tokens=self.cfg.run.budget_tokens, budget_usd=self.cfg.run.budget_usd,
            wall_deadline=now + timedelta(seconds=wall),
        )
        await sess_repo.insert(conn, s)
        if preferences_text:
            await fb_repo.insert(conn, _human_preference(s.id, preferences_text))
        return s

    async def _parse_goal(
        self,
        deps: AgentDeps,
        session: Session,
        goal: str,
        preferences_text: str | None,
    ) -> ResearchPlan:
        prompt = render(
            "parse_goal", goal=goal,
            preferences_text=preferences_text or "",
        )
        r = route(self.cfg, "parse_goal", None)
        from ..inputs import model_images

        images = await model_images(deps.db, self.cfg, session.config_snapshot.get("web_launch", {}).get("upload_group_id"))
        spec = AgentCallSpec(
            route=r,
            system_blocks=[CachedBlock("You parse research goals into structured plans.", cache=True)],
            user_blocks=[CachedBlock(prompt, cache=False)],
            tools=[RECORD_RESEARCH_PLAN_TOOL],
            tool_choice={"type": "tool", "name": "record_research_plan"},
            max_output_tokens=1024,
            input_images=images,
        )
        ctx = CallContext(
            session_id=session.id, task_id=None,
            agent="parse_goal", action="parse_goal", mode=None,
        )
        resp = await deps.llm.call(spec, ctx)
        record: dict[str, Any] | None = None
        for b in resp.raw.content:
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", "") == "record_research_plan":
                inp = getattr(b, "input", None)
                if isinstance(inp, dict):
                    record = inp
                    break
        if record is None:
            log.warning("parse_goal_no_record", note="falling back to bare ResearchPlan")
            from ..safety.evidence import infer_molecule

            target = self.cfg.run.target_molecule
            return ResearchPlan(objective=goal.strip(), preferences=[], idea_attributes=[],
                                target_molecule=infer_molecule(goal) if target == "unspecified" else target)
        from ..safety.evidence import infer_molecule

        target = self.cfg.run.target_molecule
        if target == "unspecified":
            target = infer_molecule(goal)
        return ResearchPlan(
            target_molecule=target,
            objective=record.get("objective", goal.strip()),
            preferences=record.get("preferences", []),
            constraints=record.get("constraints", []),
            idea_attributes=record.get("idea_attributes", []),
            domain_hint=record.get("domain_hint") or None,
            notes=record.get("notes") or None,
        )

    async def _apply_plan(
        self, conn: aiosqlite.Connection, session: Session, plan: ResearchPlan
    ) -> None:
        await conn.execute(
            "UPDATE sessions SET research_plan=?, updated_at=? WHERE id=?",
            (plan.model_dump_json(), datetime.now(UTC).isoformat(), session.id),
        )
        await conn.commit()

    # ----------------------------- main loop ----------------------------- #

    async def _main_loop(
        self,
        conn: aiosqlite.Connection,
        deps: AgentDeps,
        session: Session,
        tracker: StabilityTracker,
    ) -> StopReason | None:
        sem = asyncio.Semaphore(self.cfg.run.concurrency)
        inflight: set[asyncio.Task] = set()
        worker_seq = 0
        last_decide_at = 0.0
        last_snapshot_match_count = -1
        last_reclaim_at = 0.0

        async def _run_task(t: Task) -> None:
            # A transaction/cursor belongs to one worker, never to concurrent
            # agents and scheduler claims on the same aiosqlite connection.
            worker_conn = await db_mod.connect(self.cfg)
            worker_deps = AgentDeps(self.cfg, worker_conn, deps.llm, deps.tools)
            try:
                await _run_task_on_connection(t, worker_conn, self._build_agents(worker_deps))
            finally:
                await worker_conn.close()

        async def _run_task_on_connection(t: Task, conn, agents) -> None:
            bind(session_id=session.id, task_id=t.id, agent=t.agent)
            async with sem:
                await task_repo.mark_in_progress(conn, t.id)
                await self._emit(conn, session.id, "task_started",
                                 {"task_id": t.id, "agent": t.agent, "action": t.action,
                                  "target": t.target_id})
                agent = agents.get(t.agent)
                if agent is None:
                    await task_repo.fail(conn, t.id, error=f"no agent: {t.agent}",
                                          max_attempts=self.cfg.lease.max_attempts)
                    return
                async def keep_lease():
                    interval = max(0.01, min(self.cfg.lease.heartbeat_seconds, self.cfg.lease.default_seconds / 3))
                    while True:
                        await asyncio.sleep(interval)
                        await task_repo.heartbeat(conn, t.id, self.cfg.lease.default_seconds)

                heartbeat = asyncio.create_task(keep_lease())
                try:
                    async with conn.execute("SELECT result_json FROM tasks WHERE id=?", (t.id,)) as cur:
                        stored = await cur.fetchone()
                    if stored and stored["result_json"]:
                        result = TaskResult.model_validate_json(stored["result_json"])
                    else:
                        with activity_task(t):
                            result = await agent.execute(t)
                        result = TaskResult.model_validate(result)
                        await conn.execute("UPDATE tasks SET result_json=? WHERE id=?", (result.model_dump_json(), t.id))
                        await conn.commit()
                    await self._apply_follow_ups(conn, session, t, result)
                    await task_repo.complete(conn, t.id)
                except asyncio.CancelledError:
                    await conn.execute("UPDATE tasks SET status='cancelled',finished_at=? WHERE id=? AND status IN ('leased','in_progress')",
                                       (datetime.now(UTC).isoformat(), t.id))
                    await conn.commit()
                    raise
                except Exception as e:
                    error = safe_text(self.cfg, e, limit=2000)
                    failure_status = await task_repo.fail(conn, t.id, error=error,
                                          max_attempts=self.cfg.lease.max_attempts)
                    log.error("task_failed", err=error, error_type=type(e).__name__, task_id=t.id, action=t.action)
                    await self._emit(conn, session.id, "task_failed",
                                     {"task_id": t.id, "agent": t.agent, "action": t.action,
                                      "attempt": t.attempts + 1, "status": failure_status,
                                      "err": error[:300]})
                    return
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                await self._emit(conn, session.id, "task_completed",
                                 {"task_id": t.id, "agent": t.agent, "action": t.action,
                                  "kind": result.kind,
                                  "follow_hypothesis_ids": result.hypothesis_ids[:5],
                                  "review_ids": result.review_ids[:5],
                                  "match_ids": result.match_ids[:5]})

        try:
            while True:
                if time.monotonic() - last_reclaim_at >= 1:
                    await task_repo.reclaim_expired_leases(conn, session.id, max_attempts=self.cfg.lease.max_attempts)
                    last_reclaim_at = time.monotonic()
                # Check external pause/abort by re-reading session status.
                refreshed = await sess_repo.fetch(conn, session.id)
                external_stop = refreshed is not None and refreshed.status in ("aborted",)
                if refreshed is not None and refreshed.status == "paused":
                    # Wait until unpaused (or aborted).
                    await asyncio.sleep(1.0)
                    continue

                # Termination check (refreshes budget_used_* from the row)
                if refreshed is not None:
                    stop = should_stop(self.cfg, refreshed, tracker, external_stop=external_stop)
                    if stop is not None:
                        # Stop in-flight model/tool work on explicit abort or limits.
                        if inflight:
                            for worker in inflight:
                                worker.cancel()
                            await asyncio.gather(*inflight, return_exceptions=True)
                            inflight.clear()
                        return stop

                # Refill worker slots.
                slots_open = self.cfg.run.concurrency - len(inflight)
                claimed: list[Task] = []
                for _ in range(slots_open):
                    t = await task_repo.claim_one(
                        conn, session.id, worker_id=f"w{worker_seq}",
                        lease_seconds=self.cfg.lease.default_seconds,
                    )
                    if t is None:
                        break
                    worker_seq += 1
                    claimed.append(t)
                for t in claimed:
                    inflight.add(asyncio.create_task(_run_task(t)))

                # Update stability snapshot when match count crossed the threshold.
                snap = await snapshot_top_k(conn, session.id, self.cfg.termination.elo_stability_k)
                if (
                    snap.match_count >= last_snapshot_match_count + self.cfg.termination.match_snapshot_every
                ):
                    tracker.push(snap)
                    last_snapshot_match_count = snap.match_count
                    log.info(
                        "elo_snapshot", match_count=snap.match_count,
                        top_ids=list(snap.top_ids), top_elos=list(snap.top_elos),
                    )

                # If nothing to do at all and the queue is empty, run decide_next_steps
                # at most every ~10s, else exit (only if we have no hypotheses yet either).
                if not inflight and not claimed:
                    pending = await task_repo.count_by_status(conn, session.id)
                    if pending.get("leased", 0) or pending.get("in_progress", 0):
                        await asyncio.sleep(0.1)
                        continue  # another live Supervisor may own these tasks
                    if pending.get("awaiting_approval", 0):
                        await sess_repo.set_status(conn, session.id, "paused")
                        return StopReason.EXTERNAL
                    if pending.get("pending", 0) == 0:
                        now = time.monotonic()
                        if now - last_decide_at >= 10.0:
                            last_decide_at = now
                            scheduled = await self._decide_next_steps(conn, session)
                            if scheduled == 0:
                                # truly idle and no progress possible — exit gracefully
                                return StopReason.IDLE
                            continue
                        # Wait briefly so we don't spin
                        await asyncio.sleep(1.0)
                        continue

                if not inflight:
                    # Nothing claimed AND nothing running — but tasks may be pending
                    # in other workers' future claims; brief sleep and retry.
                    await asyncio.sleep(0.1)
                    continue

                _done, pending = await asyncio.wait(
                    inflight, return_when=asyncio.FIRST_COMPLETED, timeout=0.1,
                )
                for done in _done:
                    done.result()  # never silently discard an unexpected worker failure
                inflight = set(pending)
        finally:
            if inflight:
                for worker in inflight:
                    worker.cancel()
                await asyncio.gather(*inflight, return_exceptions=True)

    # ----------------------------- follow-up rules ----------------------------- #

    async def _apply_follow_ups(
        self,
        conn: aiosqlite.Connection,
        session: Session,
        task: Task,
        result,
    ) -> None:
        if result.kind == "hypothesis_created":
            for hid in result.hypothesis_ids:
                await task_repo.enqueue(conn, Task(
                    id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC),
                    agent="reflection", action="ReviewHypothesis",
                    target_id=hid, payload={"kind": "full"},
                    priority=100, status="pending",
                    idempotency_key=f"{hid}::review::full",
                ))
            requests = result.extra.get("protein_design_requests") or []
            singular = result.extra.get("protein_design_request")
            if singular and not requests:
                requests = [singular]
            for index, request in enumerate(requests):
                target_id = request.get("hypothesis_id") if isinstance(request, dict) else None
                target_id = target_id or (result.hypothesis_ids[index] if index < len(result.hypothesis_ids) else None)
                if target_id and target_id in result.hypothesis_ids:
                    await self._enqueue_protein_design_request(conn, session, target_id, request)
        elif result.kind == "review_completed":
            for hid in result.hypothesis_ids:
                await task_repo.enqueue(conn, Task(
                    id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC),
                    agent="ranking", action="AddToTournament",
                    target_id=hid, payload={}, priority=80, status="pending",
                    idempotency_key=f"{hid}::ranking::add",
                ))
        elif result.kind == "added_to_tournament":
            for hid in result.hypothesis_ids:
                await task_repo.enqueue(conn, Task(
                    id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC),
                    agent="ranking", action="RunTournamentBatch",
                    target_id=None,
                    payload={"focus": hid}, priority=120, status="pending",
                    idempotency_key=f"{hid}::ranking::focus_batch",
                ))
        elif result.kind == "tournament_match_complete":
            n_matches = result.extra.get("total_matches_after")
            _ = n_matches
            # Periodically re-cluster the proximity graph.
            from ..storage.repos import tournaments as tourney_repo

            mc = await tourney_repo.count_matches(conn, session.id)
            if (
                mc > 0
                and mc % self.cfg.vectors.full_recluster_every_matches == 0
            ):
                await task_repo.enqueue(conn, Task(
                    id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC),
                    agent="proximity", action="UpdateProximityGraph",
                    target_id=None, payload={"rebuild": True},
                    priority=200, status="pending",
                    idempotency_key=f"{session.id}::proximity::{mc}",
                ))
        elif result.kind == "protein_design_completed":
            # The existing Reflection agent is the consumer of scientific
            # evidence; Evolution then sees the resulting review as usual.
            for hid in result.hypothesis_ids:
                await task_repo.enqueue(conn, Task(
                    id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC), agent="reflection",
                    action="ReviewHypothesis", target_id=hid,
                    payload={"kind": "full", "scientific_task_id": task.id,
                             "evidence_ids": result.extra.get("evidence_ids", [])},
                    priority=125, status="pending",
                    idempotency_key=f"{hid}::review::scientific::{task.id}",
                ))

    async def _enqueue_protein_design_request(
        self, conn: aiosqlite.Connection, session: Session, hypothesis_id: str,
        raw_request: dict[str, Any],
    ) -> bool:
        """Validate and enqueue one bounded, idempotent design iteration."""
        from ..science.design_queue import enqueue_design

        return await enqueue_design(self.cfg, session.id, hypothesis_id, raw_request)

    # ----------------------------- decide_next_steps ----------------------------- #

    async def _decide_next_steps(
        self, conn: aiosqlite.Connection, session: Session
    ) -> int:
        """When the queue empties: refill it with refinement work. Returns # enqueued."""
        from ..storage.repos import tournaments as tourney_repo

        if self.cfg.run.research_mode == "lightweight":
            return await self._decide_lightweight(conn, session)

        enqueued = 0

        # We anchor idle-refinement idempotency keys on the current match count
        # rather than a fresh task id. Otherwise every idle pass — which can
        # fire every ~10s — would enqueue a *new* tournament/evolution task
        # even when a prior one is still pending, flooding the queue and
        # double-counting work toward the budget.
        anchor_mc = await tourney_repo.count_matches(conn, session.id)

        # Always: one tournament batch to keep refining Elo.
        in_tournament = await hyp_repo.list_for_session(
            conn, session.id, state="in_tournament"
        )
        if len(in_tournament) >= 2:
            inserted = await task_repo.enqueue(conn, Task(
                id=ids.task_id(), session_id=session.id,
                created_at=datetime.now(UTC),
                agent="ranking", action="RunTournamentBatch",
                target_id=None, payload={},
                priority=150, status="pending",
                idempotency_key=f"{session.id}::ranking::idle::{anchor_mc}",
            ))
            enqueued += int(inserted)

        # If the leaderboard has matured, evolve. The maturity gate and top_k are
        # config-driven so deep runs can lower the gate to grow the pool faster.
        mature = sum(1 for h in in_tournament if h.matches_played >= 3)
        if mature >= self.cfg.evolution.min_mature:
            inserted = await task_repo.enqueue(conn, Task(
                id=ids.task_id(), session_id=session.id,
                created_at=datetime.now(UTC),
                agent="evolution", action="EvolveTopHypotheses",
                target_id=None,
                payload={
                    "top_k": self.cfg.evolution.top_k,
                    "strategies": ["combine", "simplify", "out_of_box"],
                },
                priority=140, status="pending",
                idempotency_key=f"{session.id}::evolution::idle::{anchor_mc}",
            ))
            enqueued += int(inserted)

        # Periodic meta-review (every ~5 minutes wall, approximated by match count).
        mc = await tourney_repo.count_matches(conn, session.id)
        async with conn.execute(
            """SELECT COUNT(*) AS n FROM system_feedback
                  WHERE session_id=? AND kind='system_feedback' AND source='meta_review'""",
            (session.id,),
        ) as cur:
            row = await cur.fetchone()
        feedback_count = row["n"] if row else 0
        if mc >= (feedback_count + 1) * 50:
            inserted = await task_repo.enqueue(conn, Task(
                id=ids.task_id(), session_id=session.id,
                created_at=datetime.now(UTC),
                agent="metareview", action="GenerateSystemFeedback",
                target_id=None, payload={},
                priority=180, status="pending",
                idempotency_key=f"{session.id}::metareview::feedback::{feedback_count + 1}",
            ))
            enqueued += int(inserted)

        return enqueued

    async def _decide_lightweight(self, conn, session) -> int:
        from ..orchestrator.research_policy import (
            adaptive_maturity,
            eligible_pairs,
            pool_revision,
        )
        from ..storage.repos import tournaments as tourney_repo

        pending = await task_repo.count_by_status(conn, session.id)
        if any(pending.get(status, 0) for status in ("pending", "leased", "in_progress")):
            return 0
        pool = await hyp_repo.list_for_session(conn, session.id, state="in_tournament")
        all_hyps = await hyp_repo.list_for_session(conn, session.id)
        if len(pool) < 2:
            return 0
        pairs = await eligible_pairs(conn, session.id, pool,
            pair_cap=self.cfg.ranking.lightweight_max_pair_matches,
            idea_cap=self.cfg.run.max_matches_per_idea)
        revision = await pool_revision(conn, session.id, pool)
        ready = adaptive_maturity(self.cfg, pool)

        async def schedule(agent, action, key, payload=None):
            return int(await task_repo.enqueue(conn, Task(
                id=ids.task_id(), session_id=session.id, created_at=datetime.now(UTC),
                agent=agent, action=action, payload=payload or {}, priority=140,
                idempotency_key=f"{session.id}::light::{key}")))

        # Collect feedback before evolving. A duplicate key means this pool
        # was already processed; failed/duplicate evolution cannot refill it
        # forever under fresh match-count keys.
        if (ready or not pairs) and await schedule("metareview", "GenerateSystemFeedback", f"feedback::{revision}"):
            await self._emit(conn, session.id, "research_policy", {
                "label": "比较已足够; 先汇总证据缺口与系统反馈"})
            return 1
        if (ready and len(all_hyps) < self.cfg.run.max_ideas
                and await schedule("evolution", "EvolveTopHypotheses", f"evolve::{revision}", {
                    "top_k": min(self.cfg.evolution.top_k, len(pool)),
                    "strategies": ["combine", "simplify"],
                })):
            await self._emit(conn, session.id, "research_policy", {
                "label": "小样本成熟门槛已达到; 根据反馈演化假设"})
            return 1
        if pairs:
            count = await tourney_repo.count_matches(conn, session.id)
            # Include invalid matches in the key, as they also consume the pair
            # budget. Otherwise one invalid round would permanently block it.
            async with conn.execute("SELECT COUNT(*) AS n FROM tournament_matches WHERE session_id=?",
                                    (session.id,)) as cur:
                attempts = (await cur.fetchone())["n"]
            return await schedule("ranking", "RunTournamentBatch", f"ranking::{count}::{attempts}")
        await self._emit(conn, session.id, "research_policy", {
            "label": "当前假设池已达到比较上限; 结束重复比较并生成报告"})
        return 0

    # ----------------------------- finalize ----------------------------- #

    async def _finalize(
        self,
        conn: aiosqlite.Connection,
        deps: AgentDeps,
        session: Session,
        stop_reason: StopReason | None,
    ) -> None:
        if stop_reason == StopReason.EXTERNAL:
            return  # a paused/aborted session must not be marked done by Meta-review
        n_cancel = await task_repo.cancel_pending_for_session(conn, session.id)
        if n_cancel:
            log.info("pending_cancelled", n=n_cancel)

        # Try to run the proper final overview via metareview if the agent exists.
        # Fall back to the stub if metareview is not yet wired in (older builds).
        try:
            from .metareview import MetaReviewAgent

            agent = MetaReviewAgent(deps)
            final_task = Task(
                id=ids.task_id(), session_id=session.id,
                created_at=datetime.now(UTC),
                agent="metareview", action="GenerateFinalResearchOverview",
                target_id=None, payload={}, priority=1, status="pending",
                idempotency_key=f"{session.id}::metareview::final",
            )
            await task_repo.enqueue(conn, final_task)
            await task_repo.mark_in_progress(conn, final_task.id)
            await self._emit(conn, session.id, "task_started", {
                "task_id": final_task.id, "agent": "metareview", "action": final_task.action,
            })
            try:
                with activity_task(final_task):
                    result = await agent.execute(final_task)
                overview_path = result.extra.get("overview_path")
                if overview_path:
                    await sess_repo.set_final_overview(conn, session.id, overview_path)
                await task_repo.complete(conn, final_task.id)
                await self._emit(conn, session.id, "task_completed", {
                    "task_id": final_task.id, "agent": "metareview",
                    "action": final_task.action, "kind": result.kind,
                })
            except Exception as e:
                log.exception("final_overview_failed", err=str(e))
                await task_repo.fail(conn, final_task.id, error=str(e),
                                      max_attempts=self.cfg.lease.max_attempts)
                await self._emit(conn, session.id, "task_failed", {
                    "task_id": final_task.id, "agent": "metareview", "action": final_task.action,
                    "err": str(e)[:300], "status": "fallback",
                })
                overview_path = await self._write_simple_overview(conn, session)
                await sess_repo.set_final_overview(conn, session.id, overview_path)
        except ImportError:
            overview_path = await self._write_simple_overview(conn, session)
            await sess_repo.set_final_overview(conn, session.id, overview_path)

        # `set_final_overview` flips status to 'done' atomically. If the
        # overview path was never set (e.g. metareview crashed and the simple
        # overview also failed) the status is still 'running'; force-set it
        # here so the session doesn't appear to be running forever after exit.
        # For EXTERNAL stops we don't overwrite the user-set 'paused' /
        # 'aborted' status.
        if stop_reason != StopReason.EXTERNAL:
            await sess_repo.set_status(conn, session.id, "done")

        await self._emit(conn, session.id, "session_done",
                         {"stop_reason": stop_reason.value if stop_reason else None})

    async def _write_simple_overview(
        self, conn: aiosqlite.Connection, session: Session
    ) -> str:
        hyps = await hyp_repo.list_for_session(conn, session.id)
        parts: list[str] = [
            f"# Research overview — session {session.id}",
            f"\n**Goal.** {session.research_goal}\n",
            f"**Hypotheses produced.** {len(hyps)}",
            "",
        ]
        for i, h in enumerate(hyps, 1):
            parts.append(f"## {i}. {h.title or h.id}")
            parts.append(
                f"`{h.id}` — strategy `{h.strategy}` — state `{h.state}` "
                f"— Elo `{h.elo:.0f}`" if h.elo is not None else
                f"`{h.id}` — strategy `{h.strategy}` — state `{h.state}`"
            )
            parts.append(h.summary or "(no summary)")
            reviews = await rev_repo.list_for_hypothesis(conn, h.id)
            if reviews:
                parts.append("\n**Reviews:**")
                for r in reviews:
                    parts.append(
                        f"- *{r.kind}* — verdict `{r.verdict or '?'}` "
                        f"(n={r.scores.novelty}, c={r.scores.correctness}, "
                        f"t={r.scores.testability})"
                    )
            parts.append("")
        body = "\n".join(parts)
        return await write_text(self.cfg, session.id, "final", "overview", ".md", body)

    # ----------------------------- helpers ----------------------------- #

    def _build_agents(self, deps: AgentDeps) -> dict[str, object]:
        out: dict[str, object] = {
            "generation": GenerationAgent(deps),
            "reflection": ReflectionAgent(deps),
            "ranking": RankingAgent(deps),
        }
        try:
            from .scientific_tool import ScientificToolAgent

            out["scientific_tool"] = ScientificToolAgent(deps)
        except ImportError:
            pass
        # Evolution / Proximity / Meta-review register if importable.
        try:
            from .evolution import EvolutionAgent

            out["evolution"] = EvolutionAgent(deps)
        except ImportError:
            pass
        try:
            from .proximity import ProximityAgent

            out["proximity"] = ProximityAgent(deps)
        except ImportError:
            pass
        try:
            from .metareview import MetaReviewAgent

            out["metareview"] = MetaReviewAgent(deps)
        except ImportError:
            pass
        return out

    async def _emit(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        event: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        payload = payload or {}
        def scrub(value):
            if isinstance(value, str):
                return safe_text(self.cfg, value, limit=10000)
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value
        payload = scrub(payload)
        await events_repo.emit(
            conn, session_id=session_id, task_id=payload.get("task_id"),
            agent=payload.get("agent", "supervisor"),
            event=event, payload=payload,
        )
        await GLOBAL_BUS.publish(session_id, event, payload)


# ----------------------------- helpers ----------------------------- #


def _human_preference(session_id: str, text: str):
    from ..models import SystemFeedback

    return SystemFeedback(
        id=ids.feedback_id(), session_id=session_id,
        created_at=datetime.now(UTC),
        source="human", kind="preference",
        target_id=None, text=text, active=True,
    )
