"""Reproducible A/B comparison for scientific-evidence reasoning.

This module intentionally does not call an LLM. A real run can export the
Reflection/Ranking/Evolution outputs and feed them to the same comparator. It
therefore provides a safe offline mechanism for comparing runs before enabling
paid providers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_EVIDENCE_ID = re.compile(r"\bsci_[a-z0-9][a-z0-9_-]*\b", re.I)
_UNSUPPORTED_ACTIVITY = re.compile(
    r"\b(proves?|demonstrates?|confirmed?)\b.{0,60}\b(catalytic activity|enzyme activity|activity)\b",
    re.I,
)


@dataclass
class AgentABRecord:
    case_id: str
    condition: str  # baseline | evidence
    agent: str
    model: str
    output: str
    prompt_hash: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    evidence_ids: list[str] | None = None
    recorded_at: str = ""
    prompt: str | None = None
    budget: dict[str, Any] | None = None

    def __post_init__(self):
        for name in ("input_tokens", "output_tokens", "cost_usd"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError(f"invalid {name}")
            if name.endswith("tokens") and value is not None and int(value) != value:
                raise ValueError(f"{name} must be integral")

    def normalized(self) -> AgentABRecord:
        return AgentABRecord(
            **{**asdict(self),
               "prompt_hash": self.prompt_hash or (hashlib.sha256(self.prompt.encode()).hexdigest() if self.prompt is not None else ""),
               "evidence_ids": sorted(set(_EVIDENCE_ID.findall(self.output)))},
        )


def compare_records(baseline: AgentABRecord, evidence: AgentABRecord) -> dict[str, Any]:
    """Compare two same-budget records without interpreting scientific quality."""
    b = baseline.normalized()
    e = evidence.normalized()
    return {
        "case_id": b.case_id,
        "agent": b.agent,
        "baseline": asdict(b),
        "evidence": asdict(e),
        "same_model": b.model == e.model if b.model and e.model else None,
        "same_budget": b.budget == e.budget if b.budget and e.budget else None,
        "same_usage": (b.input_tokens, b.output_tokens, b.cost_usd) == (e.input_tokens, e.output_tokens, e.cost_usd)
            if all(v is not None for v in (b.input_tokens, b.output_tokens, b.cost_usd, e.input_tokens, e.output_tokens, e.cost_usd)) else None,
        "output_changed": b.output != e.output,
        "baseline_evidence_references": sorted(set(b.evidence_ids or [])),
        "evidence_references": sorted(set(e.evidence_ids or [])),
        "evidence_reference_count": len(set(e.evidence_ids or [])),
        "unsupported_activity_claim_baseline": bool(_UNSUPPORTED_ACTIVITY.search(b.output)),
        "unsupported_activity_claim_evidence": bool(_UNSUPPORTED_ACTIVITY.search(e.output)),
        "interpretation": "Mechanism-level comparison only; this does not establish scientific performance.",
    }


def compare_case(payload: dict[str, Any]) -> dict[str, Any]:
    case_id = str(payload.get("case_id", "unnamed"))
    baseline = payload.get("baseline") or {}
    evidence = payload.get("evidence") or {}
    if not isinstance(baseline, dict) or not isinstance(evidence, dict) or set(baseline) != set(evidence):
        raise ValueError("baseline and evidence must contain the same agents")
    agents = sorted(set(baseline) | set(evidence))
    comparisons = []
    for agent in agents:
        b = baseline.get(agent) or {}
        e = evidence.get(agent) or {}
        comparisons.append(compare_records(
            AgentABRecord(case_id, "baseline", agent, str(b.get("model", "")), str(b.get("output", "")),
                          prompt_hash=str(b.get("prompt_hash", "")), prompt=b.get("prompt"), budget=b.get("budget"),
                          input_tokens=b.get("input_tokens"), output_tokens=b.get("output_tokens"), cost_usd=b.get("cost_usd"), evidence_ids=b.get("evidence_ids")),
            AgentABRecord(case_id, "evidence", agent, str(e.get("model", "")), str(e.get("output", "")),
                          prompt_hash=str(e.get("prompt_hash", "")), prompt=e.get("prompt"), budget=e.get("budget"),
                          input_tokens=e.get("input_tokens"), output_tokens=e.get("output_tokens"), cost_usd=e.get("cost_usd"), evidence_ids=e.get("evidence_ids")),
        ))
    return {"case_id": case_id, "comparisons": comparisons,
            "limitations": "Citation/claim flags are syntactic checks; human scientific review and real outcome data are required."}


def compare_file(input_path: str, output_path: str | None = None) -> dict[str, Any]:
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    result = compare_case(payload)
    if output_path:
        out = Path(output_path).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result
