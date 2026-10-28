"""Structured evidence produced by scientific tools.

The model deliberately keeps computational evidence separate from literature
citations, Elo scores, and experimental activity.  A metric is useful to an
agent only when its provenance points back to a persisted artifact.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal[
    "ap_novo_metric", "structure_prediction", "campaign_manifest",
    "execution_log", "experimental", "mock",
]
EvidenceStatus = Literal["real", "mock", "unavailable", "error"]


def valid_metric(value: Any) -> bool:
    """Finite numeric model output, including arrays; booleans are not scores."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, list):
        return bool(value) and all(valid_metric(item) for item in value)
    return False


class ScientificEvidence(BaseModel):
    id: str
    session_id: str
    task_id: str
    hypothesis_id: str | None = None
    design_id: str | None = None
    design_iteration: int | None = None
    kind: EvidenceKind
    status: EvidenceStatus
    metric_name: str
    metric_value: Any = None
    unit: str | None = None
    uncertainty: Any = None
    model_version: str | None = None
    config_sha256: str | None = None
    source_path: str
    artifact_path: str
    source_role: str = "alphaprotein_novo"
    evaluation_condition: str | None = None
    notes: str | None = None
    parent_ids: list[str] = Field(default_factory=list)
    created_at: datetime
