"""Structured requests that cross the Co-Scientist/AP Novo boundary."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProteinDesignRequest(BaseModel):
    """A user- or agent-proposed design job.

    The request contains references to inputs, never generated scientific
    values.  The adapter must validate the referenced manifest before a task is
    submitted.
    """

    manifest_path: str
    output_dir: str
    mode: Literal["validate", "mock", "real"]
    rationale: str = ""
    parent_hypothesis_id: str | None = None
    design_iteration: int = Field(default=1, ge=1)
    config_version: str = ""
    requires_approval: bool = False
    input_sha256: str = ""
