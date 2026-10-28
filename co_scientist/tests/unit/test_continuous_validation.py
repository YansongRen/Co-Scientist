"""Adversarial regressions; isolated SQLite and no hosted model/tool calls."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.ranking import RankingAgent
from co_scientist.agents.reflection import ReflectionAgent
from co_scientist.agents.scientific_tool import ScientificToolAgent
from co_scientist.agents.supervisor import Supervisor
from co_scientist.evals.scientific_evidence_ab import compare_case
from co_scientist.llm.budgets import BudgetExceeded
from co_scientist.models import (
    Hypothesis,
    ResearchPlan,
    Review,
    ReviewScores,
    ScientificEvidence,
    Session,
    Task,
    TaskResult,
)
from co_scientist.orchestrator.termination import StabilityTracker, StopReason
from co_scientist.science.alphaprotein_novo import AlphaProteinNovoAdapter, evidence_from_campaign
from co_scientist.storage import artifacts, db
from co_scientist.storage.repos import hypotheses, reviews, sessions, tasks
from co_scientist.storage.repos import scientific_evidence as evidence
from co_scientist.tools.alphaprotein_novo import AlphaProteinNovoTool
from co_scientist.tools.base import ToolCtx
from co_scientist.web.app import create_app
from co_scientist.web.sanitize import sanitize_html


async def seed(conn, sid="cv-session", hid="cv-hyp"):
    now = datetime.now(UTC)
    session = Session(id=sid, created_at=now, updated_at=now, status="running",
                      research_goal="Kemp example", research_plan=ResearchPlan(objective="Kemp example"),
                      config_snapshot={}, budget_tokens=100000, budget_usd=1)
    await sessions.insert(conn, session)
    await hypotheses.insert(conn, Hypothesis(id=hid, session_id=sid, created_at=now,
        created_by="generation", strategy="literature", title=hid, summary=hid,
        full_text="Preserve the supplied motif", artifact_path="artifacts/test.json"))
    return session


def manifest(tmp_path):
    (tmp_path / "motif.cif").write_text("data_OFFLINE_FIXTURE\n")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"defaults": {"input_file": "motif.cif", "motif_str": "A1", "motif_atoms": "A1:NE2"},
        "designs": [{"name": "kemp", "num_designs": 1}], "folding": {"seeds": [230]},
        "evaluation": {"suite": "kemp_eliminase"}}))
    return path


async def add_task(conn, sid="cv-session", hid="cv-hyp", tid="cv-task", **kwargs):
    t = Task(id=tid, session_id=sid, created_at=datetime.now(UTC), agent="scientific_tool",
             action="RunProteinDesign", target_id=hid, **kwargs)
    await tasks.enqueue(conn, t)
    return t


def row(**overrides):
    return ScientificEvidence(**{
        "id": "sci_cv", "session_id": "cv-session", "task_id": "cv-task", "hypothesis_id": "cv-hyp",
        "design_id": "design_1", "kind": "ap_novo_metric", "status": "real", "metric_name": "pocket_rmsd",
        "metric_value": 0.84, "source_path": "eval.json", "artifact_path": "artifact.json",
        "created_at": datetime.now(UTC), **overrides})


def test_reinspect_mock_must_not_promote_to_real(tmp_cfg, tmp_path):
    adapter = AlphaProteinNovoAdapter(tmp_cfg)
    out = tmp_path / "campaign"
    adapter.mock_campaign(str(manifest(tmp_path)), str(out))
    assert adapter.inspect_campaign(str(out))["status"] == "mock"


def test_missing_campaign_is_unavailable(tmp_cfg, tmp_path):
    assert AlphaProteinNovoAdapter(tmp_cfg).inspect_campaign(str(tmp_path / "missing"))["status"] == "unavailable"


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "NaN", "not-a-number", True])
def test_invalid_metric_is_not_real_evidence(value):
    result = evidence_from_campaign({"status": "real", "evaluations": [{"path": "eval.json",
        "payload": {"sample_name": "d", "metrics": {"bad": value}}}]},
        session_id="s", task_id="t", hypothesis_id="h", artifact_path="a.json")
    assert not any(r.status == "real" and r.metric_name == "bad" for r in result)


@pytest.mark.asyncio
async def test_cross_session_evidence_rejected(conn):
    await seed(conn)
    await seed(conn, "other-session", "other-hyp")
    await add_task(conn)
    with pytest.raises(ValueError, match="session"):
        await evidence.insert_many(conn, [row(hypothesis_id="other-hyp")])


@pytest.mark.asyncio
async def test_evidence_history_cannot_be_overwritten(conn):
    await seed(conn)
    await add_task(conn)
    await evidence.insert_many(conn, [row()])
    with pytest.raises(ValueError, match="immutable"):
        await evidence.insert_many(conn, [row(metric_value=99)])
    assert (await evidence.list_for_task(conn, "cv-task"))[0].metric_value == 0.84


@pytest.mark.asyncio
async def test_condition_and_unit_must_not_be_deduplicated(conn):
    await seed(conn)
    await add_task(conn)
    await evidence.insert_many(conn, [row(id="sci_a", evaluation_condition="seed=1", unit="angstrom"),
        row(id="sci_b", evaluation_condition="seed=2", unit="angstrom"),
        row(id="sci_c", evaluation_condition="seed=1", unit="nm")])
    assert len(await evidence.list_unique_for_hypotheses(conn, ["cv-hyp"])) == 3


@pytest.mark.asyncio
async def test_nonfinite_legacy_row_excluded_from_formal_ranking(tmp_cfg, conn):
    await seed(conn)
    await add_task(conn)
    await evidence.insert_many(conn, [row(metric_value=float("nan"))])
    text = await RankingAgent(AgentDeps(tmp_cfg, conn, None, None))._scientific_evidence_context(["cv-hyp"])
    assert "value=nan" not in text
    assert "evidence_use=eligible" not in text


@pytest.mark.asyncio
async def test_request_replay_without_parent_does_not_create_next_iteration(tmp_cfg, conn, tmp_path):
    session = await seed(conn)
    req = {"manifest_path": str(manifest(tmp_path)), "output_dir": str(tmp_path / "out"), "mode": "mock"}
    sup = Supervisor(tmp_cfg)
    assert await sup._enqueue_protein_design_request(conn, session, "cv-hyp", req)
    assert not await sup._enqueue_protein_design_request(conn, session, "cv-hyp", req)
    async with conn.execute("SELECT COUNT(*) FROM tasks WHERE action='RunProteinDesign'") as cur:
        assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_concurrent_session_task_limit_is_atomic(tmp_cfg, conn, tmp_path):
    session = await seed(conn)
    now = datetime.now(UTC)
    for n in range(1, 8):
        await hypotheses.insert(conn, Hypothesis(id=f"h-{n}", session_id=session.id, created_at=now,
            created_by="generation", strategy="literature", title="h", summary="h", full_text="h", artifact_path="h.json"))
    tmp_cfg.alpha_protein_novo.max_design_tasks_per_session = 2
    req = {"manifest_path": str(manifest(tmp_path)), "output_dir": str(tmp_path / "out"), "mode": "mock"}
    conns = [await db.connect(tmp_cfg) for _ in range(7)]
    try:
        await asyncio.gather(*(Supervisor(tmp_cfg)._enqueue_protein_design_request(c, session, f"h-{n}", req)
                               for n, c in enumerate(conns, 1)))
    finally:
        for c in conns:
            await c.close()
    async with conn.execute("SELECT COUNT(*) FROM tasks WHERE action='RunProteinDesign'") as cur:
        assert (await cur.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_request_target_must_belong_to_session(tmp_cfg, conn, tmp_path):
    session = await seed(conn)
    await seed(conn, "foreign", "foreign-hyp")
    req = {"manifest_path": str(manifest(tmp_path)), "output_dir": str(tmp_path / "out"), "mode": "mock"}
    assert not await Supervisor(tmp_cfg)._enqueue_protein_design_request(conn, session, "foreign-hyp", req)


@pytest.mark.asyncio
async def test_real_tool_cannot_self_approve(tmp_cfg, monkeypatch):
    tmp_cfg.alpha_protein_novo.enabled = True
    tmp_cfg.alpha_protein_novo.allow_real_runs = True
    called = []
    async def forbidden(*args, **kwargs):
        called.append(True)
        return {"status": "real"}
    monkeypatch.setattr(AlphaProteinNovoAdapter, "run_pipeline", forbidden)
    result = await AlphaProteinNovoTool(tmp_cfg).call(
        {"operation": "run_pipeline", "confirm_real_run": True}, ToolCtx(tmp_cfg))
    assert result.is_error
    assert not called


@pytest.mark.asyncio
async def test_direct_real_worker_requires_durable_human_approval(tmp_cfg, conn, tmp_path, monkeypatch):
    await seed(conn)
    tmp_cfg.alpha_protein_novo.enabled = True
    tmp_cfg.alpha_protein_novo.allow_real_runs = True
    called = []
    async def forbidden(*args, **kwargs):
        called.append(True)
        return {"status": "real"}
    monkeypatch.setattr(AlphaProteinNovoAdapter, "run_pipeline", forbidden)
    task = await add_task(conn, payload={"manifest_path": str(manifest(tmp_path)),
        "output_dir": str(tmp_path / "out"), "mode": "real", "approved": True})
    with pytest.raises(PermissionError):
        await ScientificToolAgent(AgentDeps(tmp_cfg, conn, None, None)).execute(task)
    assert not called


def review_loop():
    return SimpleNamespace(response=SimpleNamespace(raw=SimpleNamespace(content=[SimpleNamespace(
        type="tool_use", name="record_review", input={"kind": "full", "verdict": "neutral", "evidence": [],
        "novelty": 0.5, "correctness": 0.5, "notes": "fixture review"})])),
        seen_urls=set(), tool_calls=[], source_observations=[], iterations=1)


def offline_deps(cfg, conn):
    registry = MagicMock()
    registry.anthropic_tools_for.return_value = []
    return AgentDeps(cfg, conn, None, registry)


@pytest.mark.asyncio
async def test_reflection_scientific_rounds_have_distinct_history(tmp_cfg, conn, monkeypatch):
    await seed(conn)
    run = AsyncMock(side_effect=[review_loop(), review_loop()])
    monkeypatch.setattr("co_scientist.agents.reflection.run_tool_loop", run)
    agent = ReflectionAgent(offline_deps(tmp_cfg, conn))
    ids = []
    for tid in ("apn-round1", "apn-round2"):
        result = await agent.execute(Task(id=f"reflection-{tid}", session_id="cv-session", created_at=datetime.now(UTC),
            agent="reflection", action="ReviewHypothesis", target_id="cv-hyp", payload={"scientific_task_id": tid}))
        ids.extend(result.review_ids)
    assert len(set(ids)) == 2
    assert len(await reviews.list_for_hypothesis(conn, "cv-hyp")) == 2


@pytest.mark.asyncio
async def test_ranking_excludes_mock_review_indirect_contamination(tmp_cfg, conn):
    await seed(conn)
    await add_task(conn)
    await evidence.insert_many(conn, [row(status="mock")])
    path = await artifacts.write_json(tmp_cfg, "cv-session", "reviews", "simulation-review", {
        "record": {"scientific_evidence_status": ["mock"], "notes": "MOCK_SECRET_SCORE"}})
    await reviews.insert(conn, Review(id="sim-review", session_id="cv-session", hypothesis_id="cv-hyp",
        created_at=datetime.now(UTC), kind="full", scores=ReviewScores(novelty=0.99), body="MOCK_SECRET_SCORE", artifact_path=path))
    text = await RankingAgent(offline_deps(tmp_cfg, conn))._best_review("cv-hyp")
    assert text is None or "MOCK_SECRET_SCORE" not in text


@pytest.mark.asyncio
async def test_reflection_rejects_foreign_hypothesis_before_model_call(tmp_cfg, conn, monkeypatch):
    await seed(conn)
    await seed(conn, "foreign", "foreign-hyp")
    run = AsyncMock(return_value=review_loop())
    monkeypatch.setattr("co_scientist.agents.reflection.run_tool_loop", run)
    with pytest.raises(ValueError, match="session"):
        await ReflectionAgent(offline_deps(tmp_cfg, conn)).execute(Task(id="foreign-ref", session_id="cv-session",
            created_at=datetime.now(UTC), agent="reflection", action="ReviewHypothesis", target_id="foreign-hyp"))
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_session_hypothesis_route_rejects_cross_session(tmp_cfg, conn):
    await seed(conn)
    await seed(conn, "foreign", "foreign-hyp")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_cfg)), base_url="http://test") as client:
        assert (await client.get("/sessions/cv-session/hypotheses/foreign-hyp")).status_code == 404


@pytest.mark.asyncio
async def test_artifact_symlink_cannot_write_into_other_session(tmp_cfg):
    target = tmp_cfg.data_dir / "artifacts" / "foreign"
    target.mkdir(parents=True)
    (tmp_cfg.data_dir / "artifacts" / "cv-session").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        await artifacts.write_json(tmp_cfg, "cv-session", "reviews", "review1", {"x": 1})


@pytest.mark.asyncio
async def test_parallel_artifact_writes_are_atomic_and_leave_no_shared_tmp(tmp_cfg):
    paths = await asyncio.gather(*(artifacts.write_json(tmp_cfg, "cv-session", "reviews", "same-id", {"i": i}) for i in range(30)))
    assert (await artifacts.read_json(tmp_cfg, paths[0]))["i"] in range(30)


@pytest.mark.parametrize("score", [-0.01, 1.01, float("nan"), float("inf"), True])
def test_review_score_rejects_invalid_numbers(score):
    with pytest.raises(ValueError):
        ReviewScores(correctness=score)


def tracker():
    return StabilityTracker(5, 3, 25)


@pytest.mark.asyncio
async def test_abort_interrupts_running_task_and_preserves_cancelled_state(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    task = await add_task(conn)
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def slow(t):
        started.set()
        try:
            await asyncio.sleep(0.4)
            return TaskResult(kind="noop")
        except asyncio.CancelledError:
            cancelled.set()
            raise
    sup = Supervisor(tmp_cfg)
    monkeypatch.setattr(sup, "_build_agents", lambda deps: {"scientific_tool": SimpleNamespace(execute=slow)})
    async def abort():
        await started.wait()
        await sessions.set_status(conn, session.id, "aborted")
    stop, _ = await asyncio.gather(sup._main_loop(conn, offline_deps(tmp_cfg, conn), session, tracker()), abort())
    assert stop == StopReason.EXTERNAL
    assert cancelled.is_set()
    async with conn.execute("SELECT status FROM tasks WHERE id=?", (task.id,)) as cur:
        assert (await cur.fetchone())[0] == "cancelled"


@pytest.mark.asyncio
async def test_awaiting_approval_pauses_instead_of_finalizing(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    await add_task(conn, status="awaiting_approval")
    sup = Supervisor(tmp_cfg)
    decide = AsyncMock(return_value=0)
    monkeypatch.setattr(sup, "_decide_next_steps", decide)
    stop = await sup._main_loop(conn, offline_deps(tmp_cfg, conn), session, tracker())
    assert stop == StopReason.EXTERNAL
    assert (await sessions.fetch(conn, session.id)).status == "paused"
    decide.assert_not_awaited()


@pytest.mark.asyncio
async def test_followup_failure_retries_cached_agent_result(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    await add_task(conn)
    sup = Supervisor(tmp_cfg)
    execute = AsyncMock(return_value=TaskResult(kind="noop"))
    monkeypatch.setattr(sup, "_build_agents", lambda deps: {"scientific_tool": SimpleNamespace(execute=execute)})
    follow = AsyncMock(side_effect=[ValueError("followup failed"), None])
    monkeypatch.setattr(sup, "_apply_follow_ups", follow)
    monkeypatch.setattr(sup, "_decide_next_steps", AsyncMock(return_value=0))
    await sup._main_loop(conn, offline_deps(tmp_cfg, conn), session, tracker())
    async with conn.execute("SELECT status,attempts FROM tasks WHERE id='cv-task'") as cur:
        result = await cur.fetchone()
    assert result["status"] == "done"
    assert result["attempts"] == 1
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_apn_timeout_reaps_child_process(tmp_cfg, tmp_path, monkeypatch):
    (tmp_path / "run_pipeline.py").write_text("# offline placeholder\n")
    tmp_cfg.alpha_protein_novo.repo_path = str(tmp_path)
    tmp_cfg.alpha_protein_novo.timeout_seconds = 0.01
    async def communicate():
        await asyncio.sleep(0.1)
        return b"", b""
    process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=communicate),
                              kill=MagicMock(), terminate=MagicMock(), wait=AsyncMock(return_value=-9))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(TimeoutError):
        await AlphaProteinNovoAdapter(tmp_cfg).run_pipeline(str(manifest(tmp_path)), str(tmp_path / "out"), allow_real=True)
    assert process.kill.called or process.terminate.called
    process.wait.assert_awaited()


@pytest.mark.parametrize("bad", [{"defaults": 7}, {"designs": 7}, {"folding": {"seeds": 3}},
                                   {"evaluation": "oops"}, {"resequence": 2}])
def test_malformed_manifest_reports_errors_instead_of_crashing(tmp_cfg, tmp_path, bad):
    path = manifest(tmp_path)
    data = json.loads(path.read_text()) | bad
    path.write_text(json.dumps(data))
    result = AlphaProteinNovoAdapter(tmp_cfg).validate_manifest(str(path))
    assert not result.ok
    assert result.errors


def test_ab_missing_metadata_is_not_equal_budget_or_prompt_hash():
    result = compare_case({"case_id": "case", "baseline": {"reflection": {"output": "A"}},
                           "evidence": {"reflection": {"output": "B"}}})["comparisons"][0]
    assert result["same_budget"] is None
    assert result["same_model"] is None
    assert not result["baseline"]["prompt_hash"]


@pytest.mark.parametrize("usage", [-1, float("nan"), float("inf")])
def test_ab_invalid_cost_cannot_be_a_valid_record(usage):
    with pytest.raises(ValueError):
        compare_case({"baseline": {"reflection": {"cost_usd": usage}},
                      "evidence": {"reflection": {"cost_usd": usage}}})


@pytest.mark.asyncio
async def test_approval_binds_motif_content(tmp_cfg, conn, tmp_path):
    session = await seed(conn)
    req = {"manifest_path": str(manifest(tmp_path)), "output_dir": str(tmp_path / "out"), "mode": "real"}
    assert await Supervisor(tmp_cfg)._enqueue_protein_design_request(conn, session, "cv-hyp", req)
    async with conn.execute("SELECT id FROM tasks") as cur:
        tid = (await cur.fetchone())[0]
    (tmp_path / "motif.cif").write_text("data_CHANGED\n")
    with pytest.raises(ValueError, match="changed"):
        await tasks.approve(conn, tid)


@pytest.mark.asyncio
async def test_dead_tasks_still_count_towards_compute_limit(tmp_cfg, conn, tmp_path):
    session = await seed(conn)
    tmp_cfg.alpha_protein_novo.max_design_tasks_per_session = 1
    await add_task(conn, status="dead")
    req = {"manifest_path": str(manifest(tmp_path)), "output_dir": str(tmp_path / "out"), "mode": "mock"}
    assert not await Supervisor(tmp_cfg)._enqueue_protein_design_request(conn, session, "cv-hyp", req)


@pytest.mark.asyncio
async def test_three_round_lineage_stops_at_limit(tmp_cfg, conn, tmp_path):
    session = await seed(conn)
    path = manifest(tmp_path)
    sup = Supervisor(tmp_cfg)
    tmp_cfg.alpha_protein_novo.max_design_iterations = 2
    req = {"manifest_path": str(path), "output_dir": str(tmp_path / "out"), "mode": "mock"}
    assert await sup._enqueue_protein_design_request(conn, session, "cv-hyp", req)
    for hid, parent in [("child", "cv-hyp"), ("grandchild", "child")]:
        await hypotheses.insert(conn, Hypothesis(id=hid, session_id=session.id, created_at=datetime.now(UTC),
            created_by="evolution", strategy="simplify", parent_ids=[parent], title=hid, summary=hid,
            full_text=hid, artifact_path="h.json"))
    assert await sup._enqueue_protein_design_request(conn, session, "child", req | {"parent_hypothesis_id": "cv-hyp"})
    assert not await sup._enqueue_protein_design_request(conn, session, "grandchild", req | {"parent_hypothesis_id": "child"})
    async with conn.execute("SELECT payload FROM tasks WHERE target_id='child'") as cur:
        assert json.loads((await cur.fetchone())[0])["design_iteration"] == 2


@pytest.mark.asyncio
async def test_cached_results_survive_connection_restart(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    task = await add_task(conn)
    await conn.execute("UPDATE tasks SET result_json=? WHERE id=?", (TaskResult(kind="noop").model_dump_json(), task.id))
    await conn.commit()
    fresh = await db.connect(tmp_cfg)
    try:
        sup = Supervisor(tmp_cfg)
        forbidden = AsyncMock(side_effect=AssertionError("must not rerun compute"))
        monkeypatch.setattr(sup, "_build_agents", lambda deps: {"scientific_tool": SimpleNamespace(execute=forbidden)})
        monkeypatch.setattr(sup, "_decide_next_steps", AsyncMock(return_value=0))
        await sup._main_loop(fresh, offline_deps(tmp_cfg, fresh), session, tracker())
        forbidden.assert_not_awaited()
        async with conn.execute("SELECT status FROM tasks") as cur:
            assert (await cur.fetchone())[0] == "done"
    finally:
        await fresh.close()


def test_mock_campaign_never_overwrites_existing_real_files(tmp_cfg, tmp_path):
    path = manifest(tmp_path)
    out = tmp_path / "campaign"
    (out / "04_eval").mkdir(parents=True)
    original = out / "04_eval" / "kemp_0000_evaluation.json"
    original.write_text('{"sample_name":"real-result"}')
    with pytest.raises(ValueError):
        AlphaProteinNovoAdapter(tmp_cfg).mock_campaign(str(path), str(out))
    assert json.loads(original.read_text())["sample_name"] == "real-result"


def test_campaign_symlink_does_not_import_foreign_metrics(tmp_cfg, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "foreign_evaluation.json").write_text('{"metrics":{"private_metric":0.9}}')
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "04_eval").symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError):
        AlphaProteinNovoAdapter(tmp_cfg).inspect_campaign(str(root))


@pytest.mark.parametrize("href", ["JaVaScRiPt:alert(1)", "java\nscript:alert(1)", "data:text/html,evil", "vbscript:evil"])
def test_html_href_rejects_obfuscated_active_scheme(href):
    assert "href=" not in sanitize_html(f'<a href="{href}">click</a>')


@pytest.mark.asyncio
async def test_concurrent_migration_is_serialized(tmp_cfg):
    # Existing isolated DB, repeated startup by four processes/connections.
    await asyncio.gather(*(db.init_db(tmp_cfg) for _ in range(4)))
    c = await db.connect(tmp_cfg)
    try:
        async with c.execute("PRAGMA integrity_check") as cur:
            assert (await cur.fetchone())[0] == "ok"
        async with c.execute("PRAGMA foreign_key_check") as cur:
            assert await cur.fetchall() == []
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_fresh_database_concurrent_migration(tmp_cfg, tmp_path):
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.storage.data_dir = str(tmp_path / "fresh-db")
    results = await asyncio.gather(*(db.init_db(cfg) for _ in range(4)), return_exceptions=True)
    assert results == [None] * 4


@pytest.mark.asyncio
async def test_faiss_multiple_instances_do_not_lose_updates(tmp_cfg):
    import numpy as np

    from co_scientist.vectors.store import FaissStore
    a, b = [FaissStore(tmp_cfg, "cv-vectors", dim=8) for _ in range(2)]
    await asyncio.gather(a.load_or_create(), b.load_or_create())
    await a.add("a", np.eye(8, dtype="float32")[0])
    await b.add("b", np.eye(8, dtype="float32")[1])
    await a.save()
    await b.save()
    c = FaissStore(tmp_cfg, "cv-vectors", dim=8)
    await c.load_or_create()
    assert c.n == 2
    assert c.offset_of("a") is not None and c.offset_of("b") is not None


@pytest.mark.asyncio
async def test_duplicate_vector_does_not_create_another_offset(tmp_cfg):
    import numpy as np

    from co_scientist.vectors.store import FaissStore
    store = FaissStore(tmp_cfg, "cv-vectors", dim=8)
    await store.load_or_create()
    assert await store.add("a", np.eye(8, dtype="float32")[0]) == 0
    assert await store.add("a", np.eye(8, dtype="float32")[0]) == 0
    assert store.n == 1


@pytest.mark.asyncio
async def test_scheduler_error_events_redact_configured_keys(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    tmp_cfg.secrets.OPENAI_API_KEY = "cv-fabricated-private-key"
    await add_task(conn)
    sup = Supervisor(tmp_cfg)
    async def fail(task):
        raise RuntimeError("connection failed using " + tmp_cfg.secrets.OPENAI_API_KEY)
    monkeypatch.setattr(sup, "_build_agents", lambda deps: {"scientific_tool": SimpleNamespace(execute=fail)})
    monkeypatch.setattr(sup, "_decide_next_steps", AsyncMock(return_value=0))
    await sup._main_loop(conn, offline_deps(tmp_cfg, conn), session, tracker())
    async with conn.execute("SELECT payload FROM events") as cur:
        assert tmp_cfg.secrets.OPENAI_API_KEY not in str([r[0] for r in await cur.fetchall()])
    async with conn.execute("SELECT last_error FROM tasks") as cur:
        assert tmp_cfg.secrets.OPENAI_API_KEY not in (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_claim_serializes_noncommutative_actions(tmp_cfg, conn):
    await seed(conn)
    for n in range(2):
        await tasks.enqueue(conn, Task(id=f"ranking-{n}", session_id="cv-session", created_at=datetime.now(UTC),
            agent="ranking", action="RunTournamentBatch"))
    first = await tasks.claim_one(conn, "cv-session", "w1", 60)
    assert first
    assert await tasks.claim_one(conn, "cv-session", "w2", 60) is None
    await tasks.complete(conn, first.id)
    assert await tasks.claim_one(conn, "cv-session", "w2", 60)


@pytest.mark.asyncio
async def test_meta_feedback_does_not_reintroduce_simulation(tmp_cfg, conn):
    from co_scientist.agents.metareview import MetaReviewAgent
    await seed(conn)
    await add_task(conn)
    await evidence.insert_many(conn, [row(status="mock")])
    path = await artifacts.write_json(tmp_cfg, "cv-session", "reviews", "mock-review",
                                     {"record": {"scientific_evidence_status": ["mock"]}})
    await reviews.insert(conn, Review(id="mock-review", session_id="cv-session", hypothesis_id="cv-hyp",
        created_at=datetime.now(UTC), kind="full", body="MOCK_SUPER_ENZYME", artifact_path=path))
    deps = offline_deps(tmp_cfg, conn)
    deps.llm = SimpleNamespace(call=AsyncMock(side_effect=AssertionError("only simulated reviews; no real feedback should be requested")))
    result = await MetaReviewAgent(deps).execute(Task(id="meta", session_id="cv-session", created_at=datetime.now(UTC),
        agent="metareview", action="GenerateSystemFeedback"))
    assert result.kind == "noop"
    deps.llm.call.assert_not_awaited()


def test_csv_scientific_notation_is_numeric():
    result = evidence_from_campaign({"status": "real", "summary_rows": [{"sample_name": "d", "rmsd": "1e-3"}],
        "evaluation_summary_csv": "summary.csv"}, session_id="s", task_id="t", hypothesis_id="h", artifact_path="a")
    metric = next(r for r in result if r.metric_name == "rmsd")
    assert metric.metric_value == 0.001 and metric.status == "real"


def test_confidence_preserves_full_design_id(tmp_path):
    path = tmp_path / "kemp_0000_variant_a_confidences_seed230.json"
    path.write_text('{"ptm":0.8}')
    result = evidence_from_campaign({"status": "real", "confidence_files": [str(path)]},
        session_id="s", task_id="t", hypothesis_id="h", artifact_path="a")
    assert next(r for r in result if r.metric_name == "ptm").design_id == "kemp_0000_variant_a"


@pytest.mark.asyncio
async def test_resume_restores_budget_before_provider_admission(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    await conn.execute("UPDATE sessions SET budget_used_tokens=99999 WHERE id=?", (session.id,))
    await conn.commit()
    captured = {}
    def fake_provider(cfg, *, db, budget):
        captured["budget"] = budget
        return None
    monkeypatch.setattr("co_scientist.agents.supervisor.get_provider", fake_provider)
    sup = Supervisor(tmp_cfg)
    monkeypatch.setattr(sup, "_main_loop", AsyncMock(return_value=StopReason.EXTERNAL))
    await sup.run_session("", resume_session_id=session.id)
    with pytest.raises(BudgetExceeded):
        await captured["budget"].admit("reflection", est_tokens=2, est_usd=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["generation", "evolution"])
async def test_duplicate_hypothesis_preserves_original_artifact(tmp_cfg, conn, monkeypatch, module):
    await seed(conn)
    if module == "generation":
        from co_scientist.agents.generation import GenerationAgent
        cls = GenerationAgent
    else:
        from co_scientist.agents.evolution import EvolutionAgent
        cls = EvolutionAgent
    agent = cls(offline_deps(tmp_cfg, conn))
    monkeypatch.setattr(agent, "_dedup_query", AsyncMock(return_value=(None, None)))
    record = {"title": "Original", "statement": "Preserved statement", "mechanism": "original mechanism", "citations": []}
    hid, inserted = await agent._persist("cv-session", record, strategy="literature" if module == "generation" else "simplify")
    assert inserted
    original = await hypotheses.fetch(conn, hid)
    before = await artifacts.read_json(tmp_cfg, original.artifact_path)
    _, inserted = await agent._persist("cv-session", record | {"mechanism": "changed mechanism"},
                                      strategy="literature" if module == "generation" else "simplify")
    assert not inserted
    assert await artifacts.read_json(tmp_cfg, original.artifact_path) == before


def test_config_repr_hides_secrets(tmp_cfg):
    tmp_cfg.secrets.OPENAI_API_KEY = "cv-fabricated-private-key"
    assert "cv-fabricated-private-key" not in repr(tmp_cfg)


@pytest.mark.asyncio
async def test_model_tool_cannot_write_outside_session(tmp_cfg, tmp_path):
    result = await AlphaProteinNovoTool(tmp_cfg).call({"operation": "mock_campaign",
        "manifest_path": str(manifest(tmp_path)), "output_dir": str(tmp_path / "foreign-campaign")},
        ToolCtx(tmp_cfg, session_id="cv-session"))
    assert result.is_error
    assert not (tmp_path / "foreign-campaign").exists()


@pytest.mark.asyncio
async def test_standard_idle_dedup_reports_no_scheduled_work(tmp_cfg, conn):
    session = await seed(conn)
    await hypotheses.init_tournament(conn, "cv-hyp")
    h = await hypotheses.fetch(conn, "cv-hyp")
    await hypotheses.insert(conn, h.model_copy(update={"id": "second"}))
    sup = Supervisor(tmp_cfg)
    assert await sup._decide_next_steps(conn, session) == 1
    async with conn.execute("SELECT id FROM tasks") as cur:
        await tasks.complete(conn, (await cur.fetchone())[0])
    assert await sup._decide_next_steps(conn, session) == 0


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_by_running_scheduler(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    await add_task(conn)
    t = await tasks.claim_one(conn, session.id, "old-worker", 60)
    await conn.execute("UPDATE tasks SET lease_expires_at=1 WHERE id=?", (t.id,))
    await conn.commit()
    sup = Supervisor(tmp_cfg)
    execute = AsyncMock(return_value=TaskResult(kind="noop"))
    monkeypatch.setattr(sup, "_build_agents", lambda deps: {"scientific_tool": SimpleNamespace(execute=execute)})
    monkeypatch.setattr(sup, "_decide_next_steps", AsyncMock(return_value=0))
    await sup._main_loop(conn, offline_deps(tmp_cfg, conn), session, tracker())
    async with conn.execute("SELECT status,attempts FROM tasks WHERE id=?", (t.id,)) as cur:
        value = await cur.fetchone()
    assert value["status"] == "done" and value["attempts"] == 1
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_metadata_connection_is_separate_from_scheduler(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    seen = {}
    def factory(cfg, *, db, budget):
        seen["provider"] = db
        return None
    async def mainloop(connection, deps, current, stability):
        assert connection is not seen["provider"]
        return StopReason.EXTERNAL
    monkeypatch.setattr("co_scientist.agents.supervisor.get_provider", factory)
    sup = Supervisor(tmp_cfg)
    monkeypatch.setattr(sup, "_main_loop", mainloop)
    await sup.run_session("", resume_session_id=session.id)


@pytest.mark.asyncio
async def test_apn_uses_process_group_and_spooled_logs(tmp_cfg, tmp_path, monkeypatch):
    (tmp_path / "run_pipeline.py").write_text("# no model execution\n")
    tmp_cfg.alpha_protein_novo.repo_path = str(tmp_path)
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await AlphaProteinNovoAdapter(tmp_cfg).run_pipeline(str(manifest(tmp_path)), str(tmp_path / "out"), allow_real=True)
    assert spawn.call_args.kwargs.get("start_new_session") is True
    assert spawn.call_args.kwargs["stdout"] != asyncio.subprocess.PIPE
    assert spawn.call_args.kwargs["stderr"] != asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_ranking_add_rejects_foreign_hypothesis(tmp_cfg, conn):
    await seed(conn)
    await seed(conn, "foreign", "foreign-hyp")
    with pytest.raises(ValueError, match="session"):
        await RankingAgent(offline_deps(tmp_cfg, conn)).execute(Task(id="rank-add", session_id="cv-session",
            created_at=datetime.now(UTC), agent="ranking", action="AddToTournament", target_id="foreign-hyp"))
    assert (await hypotheses.fetch(conn, "foreign-hyp")).elo is None


@pytest.mark.asyncio
async def test_conflicting_same_run_metrics_are_not_formal_evidence(conn):
    await seed(conn)
    await add_task(conn)
    await evidence.insert_many(conn, [row(id="sci_a", metric_value=0.84, source_path="eval.json"),
        row(id="sci_b", metric_value=0.85, source_path="summary.csv")])
    rendered = await evidence.render_for_agents(conn, ["cv-hyp"], include_mock=False, include_invalid=False)
    assert "evidence_use=eligible" not in rendered
    assert "conflict" in rendered.lower()


@pytest.mark.asyncio
async def test_independent_runs_keep_separate_evidence_identity(conn):
    await seed(conn)
    await add_task(conn)
    await add_task(conn, tid="cv-task-2")
    await evidence.insert_many(conn, [row(id="sci_a"), row(id="sci_b", task_id="cv-task-2")])
    assert len(await evidence.list_unique_for_hypotheses(conn, ["cv-hyp"])) == 2


@pytest.mark.asyncio
async def test_agent_evidence_labels_kind_and_conditions(conn):
    await seed(conn)
    await add_task(conn)
    await evidence.insert_many(conn, [row(kind="experimental", metric_name="kcat", unit="s^-1",
        uncertainty={"sd": 0.1}, evaluation_condition="pH=7.0;temperature=298K")])
    text = await evidence.render_for_agents(conn, ["cv-hyp"])
    assert "kind=experimental" in text
    assert "pH=7.0" in text and "uncertainty=" in text


@pytest.mark.asyncio
async def test_acceptance_round1_queue_100_jobs_eight_connections(tmp_cfg, conn):
    session = await seed(conn)
    for i in range(100):
        await add_task(conn, tid=f"load-{i}")
    conns = [await db.connect(tmp_cfg) for _ in range(8)]
    claimed = []
    async def drain(c, worker):
        while t := await tasks.claim_one(c, session.id, worker, 60):
            claimed.append(t.id)
            await tasks.mark_in_progress(c, t.id)
            await tasks.complete(c, t.id)
    try:
        await asyncio.gather(*(drain(c, f"w{i}") for i, c in enumerate(conns)))
    finally:
        for c in conns:
            await c.close()
    assert len(claimed) == len(set(claimed)) == 100
    assert await tasks.count_by_status(conn, session.id) == {"done": 100}


@pytest.mark.asyncio
async def test_acceptance_round1_active_lease_gets_heartbeat(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    await add_task(conn)
    tmp_cfg.lease.default_seconds = 1
    tmp_cfg.lease.heartbeat_seconds = 0.03
    started = asyncio.Event()
    async def work(t):
        started.set()
        await asyncio.sleep(0.2)
        return TaskResult(kind="noop")
    sup = Supervisor(tmp_cfg)
    monkeypatch.setattr(sup, "_build_agents", lambda deps: {"scientific_tool": SimpleNamespace(execute=work)})
    monkeypatch.setattr(sup, "_decide_next_steps", AsyncMock(return_value=0))
    running = asyncio.create_task(sup._main_loop(conn, offline_deps(tmp_cfg, conn), session, tracker()))
    await started.wait()
    async with conn.execute("SELECT lease_expires_at FROM tasks") as cur:
        before = (await cur.fetchone())[0]
    await asyncio.sleep(0.1)
    async with conn.execute("SELECT lease_expires_at FROM tasks") as cur:
        after = (await cur.fetchone())[0]
    assert after > before
    assert await tasks.reclaim_expired_leases(conn, session.id) == {"requeued": 0, "dead": 0}
    await running


@pytest.mark.asyncio
async def test_acceptance_round1_failure_is_bounded(tmp_cfg, conn, monkeypatch):
    session = await seed(conn)
    await add_task(conn)
    execute = AsyncMock(side_effect=ValueError("malformed output"))
    sup = Supervisor(tmp_cfg)
    monkeypatch.setattr(sup, "_build_agents", lambda deps: {"scientific_tool": SimpleNamespace(execute=execute)})
    monkeypatch.setattr(sup, "_decide_next_steps", AsyncMock(return_value=0))
    await sup._main_loop(conn, offline_deps(tmp_cfg, conn), session, tracker())
    assert execute.await_count == tmp_cfg.lease.max_attempts
    assert await tasks.count_by_status(conn, session.id) == {"dead": 1}


def test_acceptance_round1_ab_is_repeatable_and_honest():
    payload = {"case_id": "repeatable", "baseline": {"reflection": {"model": "offline", "prompt": "A",
        "output": "uncertain", "budget": {"max_tokens": 100}, "input_tokens": 5, "output_tokens": 5, "cost_usd": 0}},
        "evidence": {"reflection": {"model": "offline", "prompt": "B with evidence",
        "output": "uncertain sci_valid", "budget": {"max_tokens": 100}, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0}}}
    first, second = compare_case(payload), compare_case(payload)
    assert first == second
    r = first["comparisons"][0]
    assert r["same_budget"] is True and r["same_usage"] is False
    assert r["baseline"]["prompt_hash"] != r["evidence"]["prompt_hash"]


@pytest.mark.asyncio
async def test_acceptance_round1_legacy_migration_preserves_rows(tmp_cfg, tmp_path):
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.storage.data_dir = str(tmp_path / "legacy")
    await db.init_db(cfg)
    c = await db.connect(cfg)
    await seed(c)
    await c.execute("ALTER TABLE tasks DROP COLUMN result_json")
    await c.execute("ALTER TABLE tasks DROP COLUMN approval_sha256")
    await c.execute("DELETE FROM schema_migrations WHERE version>8")
    await c.commit()
    await c.close()
    await db.init_db(cfg)
    await db.init_db(cfg)
    c = await db.connect(cfg)
    try:
        assert (await hypotheses.fetch(c, "cv-hyp")).full_text == "Preserve the supplied motif"
        async with c.execute("PRAGMA foreign_key_check") as cur:
            assert await cur.fetchall() == []
        async with c.execute("PRAGMA integrity_check") as cur:
            assert (await cur.fetchone())[0] == "ok"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_acceptance_round1_mock_task_replay_preserves_evidence(tmp_cfg, conn, tmp_path):
    await seed(conn)
    task = await add_task(conn, payload={"manifest_path": str(manifest(tmp_path)), "output_dir": str(tmp_path / "mock"), "mode": "mock"})
    agent = ScientificToolAgent(offline_deps(tmp_cfg, conn))
    first = await agent.execute(task)
    second = await agent.execute(task)
    assert first.extra["evidence_ids"] == second.extra["evidence_ids"]
    assert len(await evidence.list_for_task(conn, task.id)) == 11


@pytest.mark.asyncio
async def test_acceptance_round2_conflicting_writers_cannot_replace_measurement(tmp_cfg, conn):
    await seed(conn)
    await add_task(conn)
    conns = [await db.connect(tmp_cfg) for _ in range(6)]
    try:
        results = await asyncio.gather(*(evidence.insert_many(c, [row(metric_value=float(i))])
                                        for i, c in enumerate(conns)), return_exceptions=True)
    finally:
        for c in conns:
            await c.close()
    assert sum(r is None for r in results) == 1
    assert all(r is None or isinstance(r, ValueError) for r in results)
    assert len(await evidence.list_for_task(conn, "cv-task")) == 1


@pytest.mark.asyncio
async def test_acceptance_round2_mixed_grades_do_not_enter_formal_prompt(conn):
    await seed(conn)
    await add_task(conn)
    await evidence.insert_many(conn, [row(id="sci_real", metric_name="real_metric"),
        row(id="sci_mock", metric_name="mock_metric", status="mock"),
        row(id="sci_error", metric_name="failed_metric", status="error"),
        row(id="sci_missing", metric_name="missing_metric", status="unavailable", metric_value=None)])
    text = await evidence.render_for_agents(conn, ["cv-hyp"], include_mock=False, include_invalid=False)
    assert "metric=real_metric" in text
    assert all(name not in text for name in ("mock_metric", "failed_metric", "missing_metric"))


@pytest.mark.asyncio
async def test_acceptance_round2_missing_llm_terminal_cannot_publish_hypothesis(tmp_cfg, conn, monkeypatch):
    from co_scientist.agents.generation import GenerationAgent
    await seed(conn)
    response = review_loop()
    response.response.raw.content = []
    monkeypatch.setattr("co_scientist.agents.generation.run_tool_loop", AsyncMock(return_value=response))
    with pytest.raises(RuntimeError, match="record_hypothesis"):
        await GenerationAgent(offline_deps(tmp_cfg, conn)).execute(Task(id="no-terminal", session_id="cv-session",
            created_at=datetime.now(UTC), agent="generation", action="CreateInitialHypotheses"))
    assert len(await hypotheses.list_for_session(conn, "cv-session")) == 1


@pytest.mark.asyncio
async def test_acceptance_round2_queued_remote_shape_is_not_completed_evidence(tmp_cfg, conn, tmp_path, monkeypatch):
    from co_scientist.science.worker import ComputeJob
    await seed(conn)
    task = await add_task(conn, payload={"manifest_path": str(manifest(tmp_path)), "output_dir": str(tmp_path / "out"), "mode": "mock"})
    monkeypatch.setattr("co_scientist.science.worker.LocalAlphaProteinNovoWorker.submit", AsyncMock(return_value=ComputeJob("j", "queued", "fake")))
    with pytest.raises(RuntimeError, match="did not complete"):
        await ScientificToolAgent(offline_deps(tmp_cfg, conn)).execute(task)
    assert await evidence.list_for_task(conn, task.id) == []


@pytest.mark.asyncio
async def test_acceptance_round2_slow_sse_is_bounded_and_unregisters():
    from co_scientist.orchestrator.events import EventBus
    bus = EventBus(max_buffer=4)
    gen = bus.subscribe("a")
    first = asyncio.create_task(anext(gen))
    await asyncio.sleep(0)
    for i in range(1000):
        await bus.publish("a", "event", {"i": i})
        await bus.publish("foreign", "unrelated")
    event = await first
    assert event.session_id == "a" and event.payload["i"] >= 996
    assert all(q.qsize() <= 4 for q in bus._subs["a"])
    await gen.aclose()
    assert bus.subscriber_count("a") == 0


@pytest.mark.asyncio
async def test_acceptance_round2_cli_uses_same_admission_and_dedup(tmp_cfg, conn, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from co_scientist import cli
    await seed(conn)
    path = manifest(tmp_path)
    monkeypatch.setattr(cli, "_common_setup", lambda *args: (tmp_cfg, None))
    args = ["protein-design", "cv-session", "--hypothesis", "cv-hyp", "--manifest", str(path),
            "--output-dir", str(tmp_path / "requested"), "--mode", "mock"]
    runner = CliRunner()
    first = await asyncio.to_thread(runner.invoke, cli.app, args)
    second = await asyncio.to_thread(runner.invoke, cli.app, args)
    assert first.exit_code == second.exit_code == 0, (first.output, second.output)
    async with conn.execute("SELECT payload FROM tasks WHERE action='RunProteinDesign'") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert str(tmp_cfg.session_artifact_dir("cv-session")) in json.loads(rows[0][0])["output_dir"]


@pytest.mark.asyncio
async def test_failed_connection_setup_closes_thread(tmp_cfg, monkeypatch):
    import aiosqlite
    connection = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(aiosqlite, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(db, "_apply_pragmas", AsyncMock(side_effect=aiosqlite.OperationalError("locked")))
    with pytest.raises(aiosqlite.OperationalError):
        await db.connect(tmp_cfg)
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_acceptance_twenty_fresh_databases_eight_startups(tmp_cfg, tmp_path):
    for n in range(20):
        cfg = tmp_cfg.model_copy(deep=True)
        cfg.storage.data_dir = str(tmp_path / f"startup-{n}")
        results = await asyncio.gather(*(db.init_db(cfg) for _ in range(8)), return_exceptions=True)
        assert results == [None] * 8
