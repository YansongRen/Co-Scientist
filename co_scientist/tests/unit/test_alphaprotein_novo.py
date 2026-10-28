"""Offline tests for the AlphaProtein Novo adapter and evidence handoff."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.scientific_tool import ScientificToolAgent
from co_scientist.agents.supervisor import Supervisor
from co_scientist.config import Config
from co_scientist.models import Hypothesis, ScientificEvidence, Task, TaskResult
from co_scientist.science.alphaprotein_novo import AlphaProteinNovoAdapter
from co_scientist.storage import db as db_mod
from co_scientist.storage.repos import scientific_evidence as evidence_repo
from co_scientist.tools.alphaprotein_novo import AlphaProteinNovoTool
from co_scientist.tools.base import ToolCtx


async def _hyp(conn, session_id, hid, parent_ids=None):
    from co_scientist.storage.repos import hypotheses
    await hypotheses.insert(conn, Hypothesis(id=hid, session_id=session_id, created_at=datetime.now(UTC),
        created_by="evolution" if parent_ids else "generation", strategy="simplify" if parent_ids else "literature",
        parent_ids=parent_ids or [], title=hid, summary=hid, full_text=hid, artifact_path="fixture.json"))


def _manifest(tmp_path):
    motif = tmp_path / "kemp_eliminase_motif.cif"
    motif.write_text("data-mock\n")
    manifest = tmp_path / "kemp_manifest.json"
    manifest.write_text(json.dumps({
        "settings": {"model_dir": "./models/apnovo_generator"},
        "defaults": {"input_file": motif.name, "motif_str": "A1|10-40/B1", "motif_atoms": "A1:NE2"},
        "designs": [{"name": "kemp_tight", "num_designs": 1}],
        "resequence": {"enabled": False},
        "folding": {"inputs": ["generated"], "seeds": [230], "states": [{"name": "monomer", "ligands": []}]},
        "evaluation": {"suite": "kemp_eliminase"},
    }))
    return manifest


def test_validate_manifest_reports_official_shape(tmp_path):
    manifest = _manifest(tmp_path)
    result = AlphaProteinNovoAdapter(Config()).validate_manifest(str(manifest))
    assert result.ok
    assert result.summary["official_stages"] == ["generation", "resequence", "folding", "evaluation"]
    assert result.manifest_sha256


def test_validate_manifest_never_invents_missing_inputs(tmp_path):
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps({"designs": [{"name": "x"}]}))
    result = AlphaProteinNovoAdapter(Config()).validate_manifest(str(manifest))
    assert not result.ok
    assert any("missing input_file" in item for item in result.errors)


def test_mock_campaign_matches_official_output_layout(tmp_path):
    manifest = _manifest(tmp_path)
    output = tmp_path / "campaign"
    result = AlphaProteinNovoAdapter(Config()).mock_campaign(str(manifest), str(output))
    assert result["status"] == "mock"
    assert (output / "pipeline_index.json").exists()
    assert (output / "04_eval" / "evaluation_summary.csv").exists()
    assert result["evaluations"][0]["payload"]["metrics"]["self_consistency/pocket_rmsd/mean"] == 0.84


@pytest.mark.asyncio
async def test_tool_real_run_requires_explicit_gate(tmp_cfg):
    tool = AlphaProteinNovoTool(tmp_cfg)
    result = await tool.call({"operation": "run_pipeline", "confirm_real_run": True}, ToolCtx(tmp_cfg))
    assert result.is_error
    assert "disabled" in (result.error_message or "") or "manifest" in (result.error_message or "")


@pytest.mark.asyncio
async def test_scientific_tool_agent_real_run_respects_config_gate(tmp_cfg, tmp_path):
    conn = await db_mod.connect(tmp_cfg)
    try:
        await conn.execute("INSERT INTO sessions(id,created_at,updated_at,status,research_goal,research_plan,config_snapshot,budget_tokens,budget_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                           ("session-real-gate", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), "running", "test", "{}", "{}", 100, 1.0))
        await conn.execute("INSERT INTO tasks(id,session_id,created_at,agent,action,payload,priority,status) VALUES (?,?,?,?,?,?,?,?)",
                           ("task-real-gate", "session-real-gate", datetime.now(UTC).isoformat(), "scientific_tool", "RunProteinDesign", "{}", 1, "in_progress"))
        await conn.commit()
        manifest = _manifest(tmp_path)
        task = Task(id="task-real-gate", session_id="session-real-gate", created_at=datetime.now(UTC),
                    agent="scientific_tool", action="RunProteinDesign", target_id=None,
                    payload={"manifest_path": str(manifest), "output_dir": str(tmp_path / "out"), "mode": "real"})
        with pytest.raises(RuntimeError, match="disabled"):
            await ScientificToolAgent(AgentDeps(tmp_cfg, conn, object(), None)).execute(task)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_scientific_tool_agent_persists_mock_evidence(tmp_cfg, tmp_path):
    manifest = _manifest(tmp_path)
    output = tmp_path / "campaign"
    conn = await db_mod.connect(tmp_cfg)
    try:
        await db_mod.init_db(tmp_cfg)
        # init_db uses a separate connection, so reconnect to see the table.
        await conn.close()
        conn = await db_mod.connect(tmp_cfg)
        task = Task(
            id="task_apn_mock", session_id="session-local", created_at=datetime.now(UTC),
            agent="scientific_tool", action="RunProteinDesign", target_id="hyp-local",
            payload={"manifest_path": str(manifest), "output_dir": str(output), "mode": "mock"},
        )
        # Seed the FK parents required by the evidence table.
        await conn.execute("INSERT INTO sessions(id,created_at,updated_at,status,research_goal,research_plan,config_snapshot,budget_tokens,budget_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                           ("session-local", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), "running", "test", "{}", "{}", 100, 1.0))
        await conn.execute("INSERT INTO hypotheses(id,session_id,created_at,created_by,strategy,parent_ids,title,summary,full_text,artifact_path,state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           ("hyp-local", "session-local", datetime.now(UTC).isoformat(), "generation", "literature", "[]", "test", "test", "test", "artifacts/test.json", "draft"))
        await conn.execute("INSERT INTO tasks(id,session_id,created_at,agent,action,payload,priority,status) VALUES (?,?,?,?,?,?,?,?)",
                           (task.id, task.session_id, task.created_at.isoformat(), task.agent, task.action, "{}", 1, "in_progress"))
        await conn.commit()
        class _LLM:
            pass
        agent = ScientificToolAgent(AgentDeps(tmp_cfg, conn, _LLM(), None))
        result = await agent.execute(task)
        assert result.kind == "protein_design_completed"
        rows = await evidence_repo.list_for_task(conn, task.id)
        assert rows and {row.status for row in rows} == {"mock"}
        assert any(row.metric_name == "self_consistency/pocket_rmsd/mean" for row in rows)
        assert all(row.design_iteration == 1 for row in rows), [row.design_iteration for row in rows]
        assert all(row.evaluation_condition == "suite=kemp_eliminase" for row in rows)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_supervisor_routes_explicit_generation_request(tmp_cfg, tmp_path):
    manifest = _manifest(tmp_path)
    conn = await db_mod.connect(tmp_cfg)
    try:
        await conn.execute("INSERT INTO sessions(id,created_at,updated_at,status,research_goal,research_plan,config_snapshot,budget_tokens,budget_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                           ("session-route", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), "running", "test", '{"objective":"test","preferences":[],"constraints":[],"idea_attributes":[]}', "{}", 100, 1.0))
        await conn.commit()
        task = Task(id="task-generation", session_id="session-route", created_at=datetime.now(UTC),
                    agent="generation", action="CreateInitialHypotheses", payload={})
        result = TaskResult(kind="hypothesis_created", hypothesis_ids=["hyp-route"], extra={
            "protein_design_request": {"manifest_path": str(manifest), "output_dir": str(tmp_path / "out"), "mode": "mock"}
        })
        from co_scientist.storage.repos import sessions as sessions_repo
        session = await sessions_repo.fetch(conn, "session-route")
        await _hyp(conn, session.id, "hyp-route")
        await Supervisor(tmp_cfg)._apply_follow_ups(conn, session, task, result)
        async with conn.execute("SELECT agent,action,target_id,payload FROM tasks WHERE agent='scientific_tool'") as cur:
            row = await cur.fetchone()
        assert row["agent"] == "scientific_tool"
        assert row["action"] == "RunProteinDesign"
        assert row["target_id"] == "hyp-route"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_supervisor_routes_evolution_design_request_with_limits_and_dedup(tmp_cfg, tmp_path):
    manifest = _manifest(tmp_path)
    conn = await db_mod.connect(tmp_cfg)
    try:
        await conn.execute("INSERT INTO sessions(id,created_at,updated_at,status,research_goal,research_plan,config_snapshot,budget_tokens,budget_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                           ("session-evolution-route", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), "running", "test", '{"objective":"test","preferences":[],"constraints":[],"idea_attributes":[]}', "{}", 100, 1.0))
        await conn.commit()
        from co_scientist.storage.repos import sessions as sessions_repo
        session = await sessions_repo.fetch(conn, "session-evolution-route")
        await _hyp(conn, session.id, "hyp-parent")
        await _hyp(conn, session.id, "hyp-child", ["hyp-parent"])
        request = {"manifest_path": str(manifest), "output_dir": str(tmp_path / "out"), "mode": "mock",
                   "parent_hypothesis_id": "hyp-parent", "design_iteration": 2}
        task = Task(id="task-evolution-route", session_id=session.id, created_at=datetime.now(UTC),
                    agent="evolution", action="EvolveTopHypotheses", payload={})
        result = TaskResult(kind="hypothesis_created", hypothesis_ids=["hyp-child"], extra={
            "protein_design_requests": [{**request, "hypothesis_id": "hyp-child"}],
        })
        supervisor = Supervisor(tmp_cfg)
        await supervisor._apply_follow_ups(conn, session, task, result)
        await supervisor._apply_follow_ups(conn, session, task, result)
        async with conn.execute("SELECT agent,action,target_id,status,payload FROM tasks WHERE agent='scientific_tool'") as cur:
            rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "RunProteinDesign"
        assert rows[0]["target_id"] == "hyp-child"
        assert rows[0]["status"] == "pending"
        assert json.loads(rows[0]["payload"])["design_iteration"] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_evidence_render_deduplicates_sources_and_excludes_mock_from_ranking(tmp_cfg):
    conn = await db_mod.connect(tmp_cfg)
    try:
        now = datetime.now(UTC)
        await conn.execute("INSERT INTO sessions(id,created_at,updated_at,status,research_goal,research_plan,config_snapshot,budget_tokens,budget_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                           ("session-evidence-render", now.isoformat(), now.isoformat(), "running", "test", "{}", "{}", 100, 1.0))
        await conn.execute("INSERT INTO hypotheses(id,session_id,created_at,created_by,strategy,parent_ids,title,summary,full_text,artifact_path,state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           ("hyp-evidence-render", "session-evidence-render", now.isoformat(), "generation", "literature", "[]", "test", "test", "test", "artifacts/test.json", "draft"))
        for task_id in ("task-evidence-json", "task-evidence-csv"):
            await conn.execute("INSERT INTO tasks(id,session_id,created_at,agent,action,payload,priority,status) VALUES (?,?,?,?,?,?,?,?)",
                               (task_id, "session-evidence-render", now.isoformat(), "scientific_tool", "RunProteinDesign", "{}", 1, "done"))
        await conn.commit()
        rows = [ScientificEvidence(id="sci-json", session_id="session-evidence-render", task_id="task-evidence-json", hypothesis_id="hyp-evidence-render", design_id="d1", kind="ap_novo_metric", status="mock", metric_name="pocket_rmsd", metric_value=0.84, source_path="d1.json", artifact_path="a.json", created_at=now),
                ScientificEvidence(id="sci-csv", session_id="session-evidence-render", task_id="task-evidence-json", hypothesis_id="hyp-evidence-render", design_id="d1", kind="ap_novo_metric", status="mock", metric_name="pocket_rmsd", metric_value=0.84, source_path="d1.csv", artifact_path="b.json", created_at=now)]
        await evidence_repo.insert_many(conn, rows)
        rendered = await evidence_repo.render_for_agents(conn, ["hyp-evidence-render"])
        formal = await evidence_repo.render_for_agents(conn, ["hyp-evidence-render"], include_mock=False, include_invalid=False)
        assert rendered.count("metric=pocket_rmsd") == 1
        assert "d1.csv" in rendered and "d1.json" in rendered
        assert "Mock evidence is present but excluded" in formal
        assert "value=0.84" not in formal
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_real_evolution_request_waits_for_human_approval(tmp_cfg, tmp_path):
    tmp_cfg.alpha_protein_novo.enabled = True
    tmp_cfg.alpha_protein_novo.allow_real_runs = True
    tmp_cfg.alpha_protein_novo.require_real_run_approval = True
    manifest = _manifest(tmp_path)
    conn = await db_mod.connect(tmp_cfg)
    try:
        now = datetime.now(UTC)
        await conn.execute("INSERT INTO sessions(id,created_at,updated_at,status,research_goal,research_plan,config_snapshot,budget_tokens,budget_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                           ("session-real-approval", now.isoformat(), now.isoformat(), "running", "test", '{"objective":"test","preferences":[],"constraints":[],"idea_attributes":[]}', "{}", 100, 1.0))
        await conn.commit()
        from co_scientist.storage.repos import sessions as sessions_repo
        from co_scientist.storage.repos import tasks as tasks_repo
        session = await sessions_repo.fetch(conn, "session-real-approval")
        await _hyp(conn, session.id, "hyp-parent")
        await _hyp(conn, session.id, "hyp-real-child", ["hyp-parent"])
        request = {"manifest_path": str(manifest), "output_dir": str(tmp_path / "out"), "mode": "real", "parent_hypothesis_id": "hyp-parent"}
        result = TaskResult(kind="hypothesis_created", hypothesis_ids=["hyp-real-child"], extra={"protein_design_requests": [{**request, "hypothesis_id": "hyp-real-child"}]})
        await Supervisor(tmp_cfg)._apply_follow_ups(conn, session, Task(id="task-real-parent", session_id=session.id, created_at=now, agent="evolution", action="EvolveTopHypotheses"), result)
        async with conn.execute("SELECT id,status FROM tasks WHERE agent='scientific_tool'") as cur:
            row = await cur.fetchone()
        assert row["status"] == "awaiting_approval"
        assert await tasks_repo.approve(conn, row["id"])
        async with conn.execute("SELECT status FROM tasks WHERE id=?", (row["id"],)) as cur:
            assert (await cur.fetchone())["status"] == "pending"
    finally:
        await conn.close()
