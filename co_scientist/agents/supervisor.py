"""Supervisor — durable task scheduler for the multi-agent system.

Responsibilities:
1. Parse the scientist's goal into a ResearchPlan.
2. Bootstrap the session (insert row, reclaim expired leases on resume).
3. Run a bounded asyncio worker pool that claims tasks from the DB-backed queue.
4. Apply follow-up scheduling rules after each task completes.
5. Periodically run `decide_next_steps` when the queue is idle:
   - Tournament refinement.
   - Configurable maturity-gated Evolution and bounded strategy Generation.
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
from ..llm.budgets import BudgetExceeded, TokenBudget
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
                await self._enforce_goal_safety(deps, session, preferences_text)

                confirmed = session.config_snapshot.get("web_launch", {}).get("confirmed_brief")
                if prepared_session_id and preferences_text and not confirmed:
                    await fb_repo.insert(conn, _human_preference(session.id, preferences_text))
                # A human-confirmed structured brief already supplies the plan.
                plan = session.research_plan if confirmed else await self._parse_goal(deps, session, goal, preferences_text)
                await self._apply_plan(conn, session, plan)
                session = await sess_repo.fetch(conn, session.id)
                assert session is not None

                if self.cfg.literature.enabled:
                    from ..literature.shared import gather_shared_literature
                    await gather_shared_literature(deps, session)
                await self._enqueue_initial_generation(conn, session, n_initial)
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
                await self._ensure_initial_generation(conn, session)

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

    async def _enforce_goal_safety(
        self, deps: AgentDeps, session: Session, preferences_text: str | None,
    ) -> None:
        from ..safety.classifier import SafetyBlocked, SafetyClassifier

        if not self.cfg.safety.enable_classifier:
            return
        passage = session.research_goal
        if preferences_text:
            passage = f"{passage}\n\n{preferences_text}"
        result = await SafetyClassifier(self.cfg).classify(
            passage, label="research_goal", llm=deps.llm, session_id=session.id,
        )
        action = result.action(self.cfg)
        await self._emit(deps.db, session.id, "safety_assessment", {
            "label": "research_goal", "safety_action": action,
            "categories": result.categories, "confidence": result.confidence,
        })
        if action == "block":
            await sess_repo.set_status(deps.db, session.id, "failed")
            await self._emit(deps.db, session.id, "safety_blocked", {
                "label": "research_goal", "categories": result.categories,
            })
            raise SafetyBlocked("research goal blocked by safety classifier")
        if action in ("warn", "quarantine"):
            log.warning(
                "goal_safety_flagged", session_id=session.id, action=action,
                categories=result.categories,
            )

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
            return ResearchPlan(objective=goal.strip(), preferences=[], idea_attributes=[])
        return ResearchPlan(
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
        progress = asyncio.Event()
        progress.set()  # recover already-ready candidates after a process restart
        budget_exhausted = asyncio.Event()

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
                    progress.set()
                except asyncio.CancelledError:
                    await conn.execute("UPDATE tasks SET status='cancelled',finished_at=? WHERE id=? AND status IN ('leased','in_progress')",
                                       (datetime.now(UTC).isoformat(), t.id))
                    await conn.commit()
                    raise
                except BudgetExceeded as e:
                    error = safe_text(self.cfg, e, limit=2000)
                    failure_status = await task_repo.fail(
                        conn, t.id, error=error,
                        max_attempts=self.cfg.lease.max_attempts, terminal=True,
                    )
                    log.error("task_budget_exceeded", err=error, kind=e.kind, task_id=t.id, action=t.action)
                    await self._emit(conn, session.id, "task_failed",
                                     {"task_id": t.id, "agent": t.agent, "action": t.action,
                                      "attempt": t.attempts + 1, "status": failure_status,
                                      "err": error[:300], "stop_reason": "budget"})
                    budget_exhausted.set()
                    return
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

                if budget_exhausted.is_set():
                    if inflight:
                        for worker in inflight:
                            worker.cancel()
                        await asyncio.gather(*inflight, return_exceptions=True)
                        inflight.clear()
                    return StopReason.BUDGET

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

                # Ready work is evaluated after completions, even while unrelated
                # workers are still busy. Use the scheduler-owned connection.
                if progress.is_set():
                    progress.clear()
                    await self._schedule_ready_refinement(conn, refreshed or session)

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
        async def enqueue_follow(next_task):
            inserted = await task_repo.enqueue(conn, next_task)
            if inserted:
                await events_repo.emit(conn, session_id=session.id, task_id=task.id,
                    agent="supervisor", event="task_handoff", payload={
                        "source_agent": task.agent, "destination_agent": next_task.agent,
                        "source_task_id": task.id, "destination_task_id": next_task.id,
                        "hypothesis_id": next_task.target_id,
                        "kind": result.kind, "delivery": "queued"})
            return inserted

        if result.kind == "hypothesis_created":
            for hid in result.hypothesis_ids:
                await enqueue_follow(Task(
                    id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC),
                    agent="reflection", action="ReviewHypothesis",
                    target_id=hid, payload={"kind": "initial"},
                    priority=75, status="pending",
                    idempotency_key=f"{hid}::review::initial",
                ))
            if result.hypothesis_ids:
                async with conn.execute("SELECT COUNT(*) FROM hypotheses WHERE session_id=?", (session.id,)) as cur:
                    pool_size = (await cur.fetchone())[0]
                if pool_size >= 2:
                    bucket = pool_size // self.cfg.vectors.proximity_refresh_interval
                    await enqueue_follow(Task(id=ids.task_id(), session_id=session.id,
                        created_at=datetime.now(UTC), agent="proximity", action="UpdateProximityGraph",
                        payload={"rebuild": True}, priority=90,
                        idempotency_key=f"{session.id}::proximity::pool::{bucket}"))
        elif result.kind == "review_completed":
            await self._continue_review(conn, session, result, enqueue_follow)
        elif result.kind == "added_to_tournament":
            for hid in result.hypothesis_ids:
                await enqueue_follow(Task(
                    id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC),
                    agent="ranking", action="RunTournamentBatch",
                    target_id=None,
                    payload={"focus": hid}, priority=85, status="pending",
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
                await enqueue_follow(Task(
                    id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC),
                    agent="proximity", action="UpdateProximityGraph",
                    target_id=None, payload={"rebuild": True},
                    priority=200, status="pending",
                    idempotency_key=f"{session.id}::proximity::{mc}",
                ))

    async def _evolution_strategies(self, conn, session_id: str) -> list[str]:
        from ..orchestrator.strategy_effect import mean_elo_by_strategy, strategy_weights, weighted_order
        elo = await mean_elo_by_strategy(conn, session_id)
        return weighted_order(
            list(self.cfg.evolution.strategies), strategy_weights(elo), f"{session_id}:evolution",
        )

    async def _improvement_choice(self, conn, session, seed: str) -> str:
        from ..orchestrator.strategy_effect import origin_weights, sample_choice
        return sample_choice(["evolution", "generation"], await origin_weights(conn, session.id), seed)

    _NEXT_REVIEW = {
        "initial": "full",
        "full": "verification",
        "verification": "observation",
        "observation": "simulation",
    }
    _STOP_VERDICTS = {
        "initial": {"disproved", "already_explained"},
        "full": {"disproved"},
        "verification": {"disproved"},
        "observation": {"disproved"},
    }

    async def _continue_review(self, conn, session, result, enqueue_follow) -> None:
        """Advance the review chain. Simulation is the last step before ranking.

        A scientific-evidence refresh reviews the same hypothesis again and does
        not restart the chain or create another tournament entry. Recurrent
        reviews only record the new critique.
        """
        if result.extra.get("scientific_task_id"):
            return
        kind = result.extra.get("review_kind") or "full"
        verdict = result.extra.get("verdict")
        if verdict in self._STOP_VERDICTS.get(kind, ()):
            for hid in result.hypothesis_ids:
                await hyp_repo.set_state_if(
                    conn, hid, new_state="rejected", expected_states=("draft", "reviewed"),
                )
            return
        nxt = self._NEXT_REVIEW.get(kind)
        if nxt:
            for hid in result.hypothesis_ids:
                await enqueue_follow(Task(
                    id=ids.task_id(), session_id=session.id, created_at=datetime.now(UTC),
                    agent="reflection", action="ReviewHypothesis", target_id=hid,
                    payload={"kind": nxt}, priority=75, status="pending",
                    idempotency_key=f"{hid}::review::{nxt}",
                ))
            return
        if kind == "simulation":
            for hid in result.hypothesis_ids:
                await enqueue_follow(Task(
                    id=ids.task_id(), session_id=session.id, created_at=datetime.now(UTC),
                    agent="ranking", action="AddToTournament", target_id=hid, payload={},
                    priority=80, status="pending",             idempotency_key=f"{hid}::ranking::add",
                ))

    async def _enqueue_initial_generation(self, conn, session, n_initial):
        from ..storage.artifacts import entity_lock
        from .generation_strategies import allocate
        async with entity_lock(self.cfg, session.id, "generation-allocation"):
            count = min(max(0, n_initial), self.cfg.run.max_ideas)
            session.config_snapshot.setdefault("research_execution", {})["n_initial"] = count
            await conn.execute("UPDATE sessions SET config_snapshot=? WHERE id=?",
                (json.dumps(session.config_snapshot), session.id))
            await conn.commit()
            inserted = 0
            for i, strategy in enumerate(allocate(self.cfg, count)):
                inserted += int(await task_repo.enqueue(conn, Task(id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC), agent="generation", action="CreateInitialHypotheses",
                    payload={"strategy": strategy, "n": 1, "generation_round": "initial", "batch_index": i},
                    priority=100, idempotency_key=f"{session.id}::generation::initial::{i}")))
            return inserted

    async def _ensure_initial_generation(self, conn, session) -> int:
        """Resume after a crash between parse_goal and the first enqueue.

        Keys are idempotent, so a partial enqueue is completed rather than
        skipped. Sessions without a recorded n_initial (imports) stay untouched.
        """
        n_initial = (session.config_snapshot.get("research_execution") or {}).get("n_initial")
        if n_initial is None:
            n_initial = (session.config_snapshot.get("web_launch") or {}).get("n_initial")
        if not n_initial:
            return 0
        return await self._enqueue_initial_generation(conn, session, int(n_initial))

    def _remaining_budget(self, session):
        deadline = session.wall_deadline
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)  # match existing termination rules
        return ((session.budget_tokens <= 0 or session.budget_used_tokens < session.budget_tokens)
            and (session.budget_usd <= 0 or session.budget_used_usd < session.budget_usd)
            and (deadline is None or datetime.now(UTC) < deadline))

    async def _schedule_idle_generation(self, conn, session):
        from .generation_strategies import allocate
        if not self.cfg.generation.idle_enabled or not self._remaining_budget(session):
            return 0
        async with conn.execute("SELECT payload,status FROM tasks WHERE session_id=? AND agent='generation' ORDER BY rowid", (session.id,)) as cur:
            rows = await cur.fetchall()
        # No spontaneous bootstrap of imported or synthetic sessions.
        initial = [r for r in rows if json.loads(r["payload"]).get("generation_round") == "initial"]
        if not initial:
            return 0
        pool = await hyp_repo.list_for_session(conn, session.id)
        space = self.cfg.run.max_ideas - len(pool)
        if space <= 0:
            return 0
        for round_index in range(self.cfg.generation.max_idle_rounds):
            earlier = [r for r in rows if json.loads(r["payload"]).get("generation_round") == round_index]
            if earlier:
                if any(r["status"] in ("pending", "leased", "in_progress") for r in earlier):
                    return 0
                continue
            count = min(space, self.cfg.generation.idle_batch_size)
            offset = len(rows)
            from ..orchestrator.strategy_effect import mean_elo_by_strategy, strategy_weights, weighted_order
            elo = await mean_elo_by_strategy(conn, session.id)
            names = weighted_order(
                list(self.cfg.generation.strategies), strategy_weights(elo),
                f"{session.id}:generation:{offset}",
            )
            inserted = 0
            for i, strategy in enumerate(allocate(self.cfg, count, offset=offset, names=names)):
                inserted += int(await task_repo.enqueue(conn, Task(id=ids.task_id(), session_id=session.id,
                    created_at=datetime.now(UTC), agent="generation", action="CreateInitialHypotheses",
                    payload={"strategy": strategy, "n": 1, "generation_round": round_index, "batch_index": i},
                    priority=160, idempotency_key=f"{session.id}::generation::idle::{round_index}::{i}")))
            return inserted
        return 0

    async def _maturity(self, conn, session, pool, *, has_pairs=True):
        from ..orchestrator.research_policy import maturity_decision
        async with conn.execute("SELECT COUNT(*) FROM tasks WHERE session_id=? AND agent='generation' AND idempotency_key LIKE ?",
            (session.id, f"{session.id}::generation::initial::%")) as cur:
            initial = (await cur.fetchone())[0]
        initial = initial or session.config_snapshot.get("research_execution", {}).get("n_initial") or session.config_snapshot.get("web_launch", {}).get("n_initial") or len(pool)
        gate = maturity_decision(self.cfg, pool, session=session, n_initial=initial, has_pairs=has_pairs)
        from ..orchestrator.termination import EloSnapshot
        evo = self.cfg.evolution
        snap = await snapshot_top_k(conn, session.id, evo.small_pool_stability_top_k)
        async with conn.execute("SELECT payload FROM events WHERE session_id=? AND event='evolution_maturity_observed' ORDER BY rowid DESC LIMIT ?",
            (session.id, evo.small_pool_stability_snapshots)) as cur:
            history = [json.loads(r[0]) for r in await cur.fetchall()]
        current = {"match_count": snap.match_count, "top_ids": list(snap.top_ids),
                   "top_elos": list(snap.top_elos), "pool_size": snap.pool_size}
        if not history or history[0] != current:
            await self._emit(conn, session.id, "evolution_maturity_observed", current)
            history.insert(0, current)
        tracker = StabilityTracker(evo.small_pool_stability_top_k, evo.small_pool_stability_snapshots,
                                   evo.small_pool_stability_epsilon)
        unique = []
        seen_counts = set()
        for item in history:
            if item["match_count"] not in seen_counts:
                unique.append(item)
                seen_counts.add(item["match_count"])
        for item in reversed(unique[:evo.small_pool_stability_snapshots]):
            tracker.push(EloSnapshot(**item))
        gate["top_stable"] = len(snap.top_ids) >= min(2, len(pool)) and tracker.is_stable()
        if gate["small_pool"] and evo.small_pool_require_top_stability and not gate["top_stable"]:
            gate["ready"] = False
        return gate

    def _streaming_enabled(self, session):
        # New sessions snapshot this policy. Do not silently change the cost /
        # scheduling contract of historical runs when they are resumed.
        return self.cfg.evolution.streaming_enabled and session.config_snapshot.get("evolution", {}).get("streaming_enabled", False)

    async def _schedule_ready_refinement(self, conn, session):
        """Local maturity -> immutable parent/review snapshot, no global barrier."""
        import hashlib

        from ..storage.artifacts import entity_lock
        if not self._streaming_enabled(session):
            return 0
        async with entity_lock(self.cfg, session.id, "idle-scheduling"):
            current = await sess_repo.fetch(conn, session.id)
            if not current or current.status != "running" or not self._remaining_budget(current):
                return 0
            async with conn.execute("SELECT status,payload FROM tasks WHERE session_id=? AND agent='evolution'", (session.id,)) as cur:
                existing = await cur.fetchall()
            if any(r["status"] in ("pending", "leased", "in_progress") for r in existing):
                return 0
            rounds = sum(json.loads(r["payload"]).get("scheduling") == "ready" for r in existing)
            if rounds >= self.cfg.evolution.max_streaming_rounds:
                return 0
            all_hyps = await hyp_repo.list_for_session(conn, session.id)
            if len(all_hyps) >= self.cfg.run.max_ideas:
                return 0
            pool = [h for h in all_hyps if h.state in ("in_tournament", "pinned")]
            gate = await self._maturity(conn, current, pool)
            if not gate["ready"]:
                return 0
            ready = [h for h in pool if h.matches_played >= gate["matches_per_mature"]]
            ready.sort(key=lambda h: (-(h.elo if h.elo is not None else 1200), h.id))
            parents, review_ids = [], {}
            for h in ready:
                reviews = await rev_repo.reasoning_reviews(self.cfg, conn, await rev_repo.list_for_hypothesis(conn, h.id))
                if reviews:
                    parents.append(h.id)
                    review_ids[h.id] = reviews[0].id
                if len(parents) >= self.cfg.evolution.top_k:
                    break
            if len(parents) < max(2, min(gate["required_mature"], self.cfg.evolution.top_k)):
                return 0
            # Rank ordering / incidental Elo fluctuations do not create rounds.
            revision = hashlib.sha256(json.dumps(sorted(review_ids.items())).encode()).hexdigest()[:24]
            task = Task(id=ids.task_id(), session_id=session.id, created_at=datetime.now(UTC),
                agent="evolution", action="EvolveTopHypotheses", priority=90,
                payload={"scheduling": "ready", "parent_ids": parents, "parent_review_ids": review_ids,
                    "top_k": len(parents), "strategies": await self._evolution_strategies(conn, session.id),
                    "maturity": gate, "candidate_revision": revision},
                idempotency_key=f"{session.id}::evolution::ready::{revision}")
            inserted = await task_repo.enqueue(conn, task)
            if inserted:
                await self._emit(conn, session.id, "task_handoff", {
                    "source_agent": "supervisor", "destination_agent": "evolution",
                    "source_task_id": None, "destination_task_id": task.id,
                    "kind": "mature_candidates_ready", "delivery": "queued",
                    "parent_ids": parents, "parent_review_ids": review_ids,
                    "round": rounds+1})
            return int(inserted)

    # ----------------------------- decide_next_steps ----------------------------- #

    async def _decide_next_steps(
        self, conn: aiosqlite.Connection, session: Session
    ) -> int:
        from ..storage.artifacts import entity_lock
        async with entity_lock(self.cfg, session.id, "idle-scheduling"):
            current = await sess_repo.fetch(conn, session.id)
            if current:
                session = current
            if session.status != "running" or not self._remaining_budget(session):
                return 0
            return await self._refine_idle(conn, session)

    async def _refine_idle(self, conn, session):
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
        from ..orchestrator.research_policy import pool_revision
        gate = await self._maturity(conn, session, in_tournament)
        all_hyps = await hyp_repo.list_for_session(conn, session.id)
        revision = await pool_revision(conn, session.id, in_tournament)
        can_evolve = (
            not self._streaming_enabled(session)
            and gate["ready"]
            and len(all_hyps) < self.cfg.run.max_ideas
        )
        choice = "evolution"
        if can_evolve:
            choice = await self._improvement_choice(
                conn, session, f"{session.id}:{anchor_mc}:{len(all_hyps)}",
            )

        async def enqueue_evolution() -> int:
            return int(await task_repo.enqueue(conn, Task(
                id=ids.task_id(), session_id=session.id,
                created_at=datetime.now(UTC),
                agent="evolution", action="EvolveTopHypotheses",
                target_id=None,
                payload={
                    "top_k": self.cfg.evolution.top_k,
                    "strategies": await self._evolution_strategies(conn, session.id),
                    "maturity": gate,
                },
                priority=140, status="pending",
                idempotency_key=f"{session.id}::evolution::pool::{revision}",
            )))

        evolution_scheduled = False
        if can_evolve and choice == "evolution":
            inserted = await enqueue_evolution()
            enqueued += inserted
            evolution_scheduled = bool(inserted)

        from ..orchestrator.research_policy import meta_feedback_due

        mc = await tourney_repo.count_matches(conn, session.id)
        async with conn.execute(
            """SELECT COUNT(*) AS n FROM system_feedback
                  WHERE session_id=? AND kind='system_feedback' AND source='meta_review'""",
            (session.id,),
        ) as cur:
            row = await cur.fetchone()
        feedback_count = row["n"] if row else 0
        if meta_feedback_due(
            mc, feedback_count, pool_size=len(in_tournament),
            every=self.cfg.metareview.feedback_every_matches,
        ):
            inserted = await task_repo.enqueue(conn, Task(
                id=ids.task_id(), session_id=session.id,
                created_at=datetime.now(UTC),
                agent="metareview", action="GenerateSystemFeedback",
                target_id=None, payload={},
                priority=180, status="pending",
                idempotency_key=f"{session.id}::metareview::feedback::{feedback_count + 1}",
            ))
            enqueued += int(inserted)

        if not evolution_scheduled:
            generated = await self._schedule_idle_generation(conn, session)
            enqueued += generated
            if generated == 0 and can_evolve and choice == "generation":
                enqueued += await enqueue_evolution()
        return enqueued

    async def _decide_lightweight(self, conn, session) -> int:
        from ..orchestrator.research_policy import (
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
            return await self._schedule_idle_generation(conn, session)
        pairs = await eligible_pairs(conn, session.id, pool,
            pair_cap=self.cfg.ranking.lightweight_max_pair_matches,
            idea_cap=self.cfg.run.max_matches_per_idea)
        revision = await pool_revision(conn, session.id, pool)
        gate = await self._maturity(conn, session, pool, has_pairs=bool(pairs))
        ready = gate["ready"]

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
        if (not self._streaming_enabled(session) and ready and len(all_hyps) < self.cfg.run.max_ideas):
            choice = await self._improvement_choice(conn, session, f"{session.id}:light:{revision}")
            if choice == "generation":
                generated = await self._schedule_idle_generation(conn, session)
                if generated:
                    await self._emit(conn, session.id, "research_policy", {
                        "label": "生成与演化都已有比赛结果; 这一轮按权重先补充新假设"})
                    return generated
            if await schedule("evolution", "EvolveTopHypotheses", f"evolve::{revision}", {
                    "top_k": min(self.cfg.evolution.top_k, len(pool)),
                    "strategies": (await self._evolution_strategies(conn, session.id))[:2],
                    "maturity": gate,
                }):
                await self._emit(conn, session.id, "research_policy", {
                    "label": "小样本成熟门槛已达到; 根据反馈演化假设"})
                return 1
        if not ready or not pairs:
            generated = await self._schedule_idle_generation(conn, session)
            if generated:
                return generated
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
        await self._record_termination(conn, session, stop_reason)
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
            except BudgetExceeded as e:
                error = safe_text(self.cfg, e, limit=2000)
                log.error("final_overview_budget", err=error, kind=e.kind)
                await task_repo.fail(conn, final_task.id, error=error,
                                      max_attempts=self.cfg.lease.max_attempts, terminal=True)
                await self._emit(conn, session.id, "task_failed", {
                    "task_id": final_task.id, "agent": "metareview", "action": final_task.action,
                    "err": error[:300], "status": "fallback", "stop_reason": "budget",
                })
                overview_path = await self._write_simple_overview(conn, session)
                await sess_repo.set_final_overview(conn, session.id, overview_path)
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

        termination = (session.config_snapshot or {}).get("termination") or {}
        await self._emit(conn, session.id, "session_done", {
            "stop_reason": stop_reason.value if stop_reason else None,
            "hypothesis_count": termination.get("hypothesis_count"),
            "output": termination.get("output"),
            "detail": termination.get("detail"),
        })

    async def _record_termination(self, conn, session, stop_reason: StopReason | None) -> None:
        hyps = await hyp_repo.list_for_session(conn, session.id)
        n = len(hyps)
        reason = stop_reason.value if stop_reason else "unknown"
        output = "none" if n == 0 else "partial" if reason == "budget" else "complete"
        labels = {
            "budget": "预算不足，已停止",
            "idle": "没有更多可调度的任务",
            "wall_clock": "到达运行时限",
            "elo_stable": "排名已稳定",
        }
        detail = labels.get(reason, "研究结束")
        if n == 0:
            detail = f"{detail}；没有产生假设"
        session.config_snapshot.setdefault("termination", {})
        session.config_snapshot["termination"] = {
            "reason": reason, "hypothesis_count": n, "output": output, "detail": detail,
        }
        await conn.execute(
            "UPDATE sessions SET config_snapshot=?, updated_at=? WHERE id=?",
            (json.dumps(session.config_snapshot), datetime.now(UTC).isoformat(), session.id),
        )
        await conn.commit()

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
        from ..obs import diversity
        diagnostics = await diversity.record(self.cfg, conn, session.id)
        body = "\n".join(parts) + "\n\n" + diversity.render(diagnostics) + "\n"
        return await write_text(self.cfg, session.id, "final", "overview", ".md", body)

    # ----------------------------- helpers ----------------------------- #

    def _build_agents(self, deps: AgentDeps) -> dict[str, object]:
        out: dict[str, object] = {
            "generation": GenerationAgent(deps),
            "reflection": ReflectionAgent(deps),
            "ranking": RankingAgent(deps),
        }
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
