"""Review models — produced by the Reflection agent."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field


def _coerce_score(value: Any) -> Any:
    """Models often emit scores as `"0.5"`; keep 0–1 numeric validation."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("score must be a number")
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        value = float(value)
    elif isinstance(value, int):
        value = float(value)
    return value


Score = Annotated[float | None, BeforeValidator(_coerce_score)]

ReviewKind = Literal["initial", "full", "verification", "observation", "simulation", "recurrent"]
ReviewVerdict = Literal[
    "already_explained", "other_more_likely", "missing_piece", "neutral", "disproved"
]


class Evidence(BaseModel):
    """Required structure for any factual claim a review makes.

    The Reflection agent's `record_review` tool requires url + excerpt,
    so the citation_verifier can fetch the URL and check the excerpt
    actually appears on the page.
    """

    claim: str
    url: str
    excerpt: str


class AssumptionCheck(BaseModel):
    """One row of the deep-verification decomposition."""

    assumption: str
    plausibility: Literal["plausible", "uncertain", "implausible"]
    rationale: str


class ReviewScores(BaseModel):
    novelty: Score = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    correctness: Score = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    testability: Score = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    feasibility: Score = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class Review(BaseModel):
    id: str
    hypothesis_id: str
    session_id: str
    created_at: datetime
    kind: ReviewKind
    verdict: ReviewVerdict | None = None
    scores: ReviewScores = Field(default_factory=ReviewScores)
    assumptions: list[AssumptionCheck] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    body: str                              # markdown
    artifact_path: str
