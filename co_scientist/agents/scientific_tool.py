"""Auxiliary worker that runs the optional scientific tool and records evidence."""

from __future__ import annotations

from ..models import Task, TaskResult
from ..science.alphaprotein_novo import AlphaProteinNovoAdapter, evidence_from_campaign
from ..science.worker import LocalAlphaProteinNovoWorker, RemoteScientificWorker
from ..storage.artifacts import write_json
from ..storage.repos import scientific_evidence as evidence_repo
from ..storage.repos.tasks import approval_digest
from .base import BaseAgent


class ScientificToolAgent(BaseAgent):
    """Keeps the six research-agent roles intact while executing long tools."""

    name = "scientific_tool"

    async def execute(self, task: Task) -> TaskResult:
        if task.action != "RunProteinDesign":
            raise ValueError(f"unsupported scientific tool action: {task.action}")
        manifest = str(task.payload.get("manifest_path", ""))
        mode = str(task.payload.get("mode", "validate"))
        if not manifest:
            raise ValueError("RunProteinDesign requires manifest_path")
        adapter = AlphaProteinNovoAdapter(self.deps.cfg)
        request = adapter.validate_design_request(task.payload, parent_hypothesis_id=task.payload.get("parent_hypothesis_id"), design_iteration=int(task.payload.get("design_iteration", 1)))
        if task.target_id:
            async with self.deps.db.execute("SELECT session_id FROM hypotheses WHERE id=?", (task.target_id,)) as cur:
                target = await cur.fetchone()
            if target is None or target["session_id"] != task.session_id:
                raise ValueError("scientific task target/session mismatch")
        if mode == "real":
            if not (self.deps.cfg.alpha_protein_novo.enabled and self.deps.cfg.alpha_protein_novo.allow_real_runs):
                raise RuntimeError("real AlphaProtein Novo runs are disabled")
            async with self.deps.db.execute("SELECT session_id,status,payload,approval_sha256 FROM tasks WHERE id=?", (task.id,)) as cur:
                durable = await cur.fetchone()
            if (durable is None or durable["session_id"] != task.session_id
                    or durable["approval_sha256"] != approval_digest(task.payload, request.input_sha256)):
                raise PermissionError("real scientific task requires durable human approval of these inputs")
        if mode != request.mode:
            raise ValueError("request mode is inconsistent")
        worker = (
            LocalAlphaProteinNovoWorker(self.deps.cfg)
            if self.deps.cfg.alpha_protein_novo.execution_backend == "local"
            else RemoteScientificWorker(self.deps.cfg.alpha_protein_novo.remote_worker_url)
        )
        job = await worker.submit(request)
        if job.status == "failed":
            raise RuntimeError(job.error or "scientific compute worker failed")
        if job.status != "completed":
            raise RuntimeError(f"worker did not complete: {job.status}")
        result = job.result or {}
        artifact_path = await write_json(
            self.deps.cfg, task.session_id, "scientific_tools/alphaprotein_novo",
            task.id, {"task": task.model_dump(mode="json"), "result": result, "status": mode},
        )
        evidence = []
        if mode in {"mock", "real"} and isinstance(result, dict):
            evidence = evidence_from_campaign(
                result, session_id=task.session_id, task_id=task.id,
                hypothesis_id=task.target_id, artifact_path=artifact_path,
                parent_ids=[request.parent_hypothesis_id] if request.parent_hypothesis_id else [],
                design_iteration=request.design_iteration,
            )
            await evidence_repo.insert_many(self.deps.db, evidence)
        return TaskResult(
            # Validation is deliberately non-computational and must not cause
            # Reflection to spend an LLM call when no design evidence exists.
            kind="protein_design_completed" if mode in {"mock", "real"} else "noop",
            hypothesis_ids=[task.target_id] if task.target_id else [],
            extra={"mode": mode, "artifact_path": artifact_path,
                   "evidence_ids": [row.id for row in evidence],
                   "evidence_count": len(evidence), "result": result},
        )
