"""Execution seam for local and future remote scientific-compute workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..models import ProteinDesignRequest
from .alphaprotein_novo import AlphaProteinNovoAdapter


@dataclass
class ComputeJob:
    job_id: str
    status: str  # queued | running | completed | failed | cancelled
    backend: str
    result: dict[str, Any] | None = None
    error: str | None = None


class ScientificComputeWorker(Protocol):
    async def submit(self, request: ProteinDesignRequest) -> ComputeJob: ...
    async def status(self, job_id: str) -> ComputeJob: ...
    async def cancel(self, job_id: str) -> bool: ...


class LocalAlphaProteinNovoWorker:
    """Synchronous-in-process implementation behind the durable task queue."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.adapter = AlphaProteinNovoAdapter(cfg)

    async def submit(self, request: ProteinDesignRequest) -> ComputeJob:
        try:
            if request.mode == "validate":
                result = self.adapter.validate_manifest(request.manifest_path).__dict__
            elif request.mode == "mock":
                result = self.adapter.mock_campaign(request.manifest_path, request.output_dir)
            else:
                result = await self.adapter.run_pipeline(
                    request.manifest_path, request.output_dir,
                    allow_real=bool(self.cfg.alpha_protein_novo.enabled and self.cfg.alpha_protein_novo.allow_real_runs),
                )
            return ComputeJob(request.output_dir, "completed", "local", result=result)
        except Exception as exc:
            return ComputeJob(request.output_dir, "failed", "local", error=str(exc))

    async def status(self, job_id: str) -> ComputeJob:
        # The Supervisor task row is the durable status source for local jobs.
        return ComputeJob(job_id, "unknown", "local")

    async def cancel(self, job_id: str) -> bool:
        # Cancellation is handled by the Supervisor lease/task layer.
        return False


class RemoteScientificWorker:
    """Contract placeholder for a future authenticated GPU-worker service.

    No HTTP calls are made by this class yet. The interface is intentionally
    job-oriented so the main server can persist a remote job ID and poll or
    cancel it without changing Agent or Evidence code.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def submit(self, request: ProteinDesignRequest) -> ComputeJob:
        raise RuntimeError("remote scientific worker is not implemented; configure a worker service first")

    async def status(self, job_id: str) -> ComputeJob:
        raise RuntimeError("remote scientific worker is not implemented")

    async def cancel(self, job_id: str) -> bool:
        raise RuntimeError("remote scientific worker is not implemented")
