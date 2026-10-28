"""Shared tool-use schemas for structured outputs.

Each agent gets one or more of these as required tool calls. Tool-use schemas
(rather than "respond in JSON") remain the most reliable structured-output
mechanism, and they are what the MCP server in `co_scientist/mcp/` serves to
the CLI backends — validated at the tool boundary, exactly as the API used to
validate them.
"""

from __future__ import annotations

from typing import Any

RECORD_HYPOTHESIS_TOOL: dict[str, Any] = {
    "name": "record_hypothesis",
    "description": (
        "Record a structured hypothesis at the end of generation/evolution. Call this "
        "exactly once when your hypothesis is finalized. All citations must reference "
        "URLs that previously appeared in your tool_result outputs from search/fetch."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title":     {"type": "string", "description": "Short noun-phrase title."},
            "statement": {"type": "string", "description": "One sentence: the hypothesis."},
            "mechanism": {"type": "string", "description": "Detailed causal/mechanistic story."},
            "entities": {
                "type": "array", "items": {"type": "string"},
                "description": "Specific named actors (proteins, materials, datasets, agents, etc.).",
            },
            "anticipated_outcomes": {
                "type": "string",
                "description": "What would be observed if the hypothesis is true.",
            },
            "novelty_argument": {
                "type": "string",
                "description": "What is new relative to the cited literature.",
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "url":     {"type": "string"},
                        "title":   {"type": "string"},
                        "excerpt": {"type": "string", "description": "Verbatim short quote from the source."},
                        "doi":     {"type": "string"},
                        "year":    {"type": "integer"},
                    },
                    "required": ["url", "title"],
                },
            },
            "strategy": {
                "type": "string",
                "enum": ["literature", "debate", "combine", "simplify",
                         "out_of_box", "feasibility", "assumption", "feedback_driven"],
                "description": "Strategy that produced this hypothesis (set by the agent).",
            },
            "parent_ids": {
                "type": "array", "items": {"type": "string"},
                "description": "Hypothesis IDs this one descends from (Evolution only).",
            },
            "protein_design_request": {
                "type": "object",
                "description": "Optional request only when the researcher supplied a complete local official AlphaProtein Novo manifest.",
                "properties": {
                    "manifest_path": {"type": "string"},
                    "output_dir": {"type": "string"},
                    "mode": {"type": "string", "enum": ["validate", "mock", "real"]},
                    "rationale": {"type": "string"},
                },
                "required": ["manifest_path", "output_dir", "mode"],
            },
        },
        "required": [
            "title", "statement", "mechanism",
            "entities", "anticipated_outcomes", "novelty_argument", "citations",
        ],
    },
}


RECORD_REVIEW_TOOL: dict[str, Any] = {
    "name": "record_review",
    "description": (
        "Record a structured review of a hypothesis. Every claim in `evidence[]` "
        "must include a URL and a verbatim excerpt; the URL must have appeared in "
        "your tool_result outputs. Pick exactly one verdict."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [
                    "already_explained",
                    "other_more_likely",
                    "missing_piece",
                    "neutral",
                    "disproved",
                ],
            },
            "kind": {
                "type": "string",
                "enum": ["full", "verification", "observation", "simulation"],
                "description": "Which review mode you ran.",
            },
            "novelty":     {"type": "number", "minimum": 0, "maximum": 1},
            "correctness": {"type": "number", "minimum": 0, "maximum": 1},
            "testability": {"type": "number", "minimum": 0, "maximum": 1},
            "feasibility": {"type": "number", "minimum": 0, "maximum": 1},
            "assumptions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "assumption":   {"type": "string"},
                        "plausibility": {"type": "string", "enum": ["plausible", "uncertain", "implausible"]},
                        "rationale":    {"type": "string"},
                    },
                    "required": ["assumption", "plausibility", "rationale"],
                },
            },
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim":   {"type": "string"},
                        "url":     {"type": "string"},
                        "excerpt": {"type": "string"},
                    },
                    "required": ["claim", "url", "excerpt"],
                },
            },
            "notes": {"type": "string", "description": "Anything that didn't fit the structured fields."},
        },
        "required": ["verdict", "kind", "evidence"],
    },
}


RECORD_SYSTEM_FEEDBACK_TOOL: dict[str, Any] = {
    "name": "record_system_feedback",
    "description": "Record a structured meta-review of the session's reviews + debates.",
    "input_schema": {
        "type": "object",
        "properties": {
            "common_weaknesses":     {"type": "array", "items": {"type": "string"}},
            "common_strengths":      {"type": "array", "items": {"type": "string"}},
            "suggested_focus_areas": {"type": "array", "items": {"type": "string"}},
            "narrative":             {"type": "string"},
        },
        "required": ["narrative"],
    },
}


RECORD_SAFETY_ASSESSMENT_TOOL: dict[str, Any] = {
    "name": "record_safety_assessment",
    "description": "Record a structured safety assessment of input text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "none",
                        "dual_use_bio",
                        "cbrn",
                        "weapons",
                        "illicit_synthesis",
                        "csam",
                    ],
                },
                "description": "All categories that apply. Use ['none'] if benign.",
            },
            "confidence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "0..1 confidence in the worst-case categorization.",
            },
            "rationale": {"type": "string"},
        },
        "required": ["categories", "confidence", "rationale"],
    },
}


RECORD_RUBRIC_SCORE_TOOL: dict[str, Any] = {
    "name": "record_rubric_score",
    "description": "Record per-criterion 1-5 scores and a brief rationale.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":      {"type": "string"},
                        "score":     {"type": "integer", "minimum": 1, "maximum": 5},
                        "rationale": {"type": "string"},
                    },
                    "required": ["name", "score", "rationale"],
                },
            },
            "overall_notes": {"type": "string"},
        },
        "required": ["scores"],
    },
}


RECORD_RESEARCH_PLAN_TOOL: dict[str, Any] = {
    "name": "record_research_plan",
    "description": "Record the parsed research plan derived from the scientist's goal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "objective":       {"type": "string"},
            "preferences":     {"type": "array", "items": {"type": "string"}},
            "constraints":     {"type": "array", "items": {"type": "string"}},
            "idea_attributes": {"type": "array", "items": {"type": "string"}},
            "domain_hint":     {"type": "string"},
            "notes":           {"type": "string"},
        },
        "required": ["objective", "preferences", "idea_attributes"],
    },
}


# Optional annotations preserve compatibility with existing stored artifacts.
# These are model judgments; deterministic quote checks are recorded separately.
_EVIDENCE_FIELDS = {
    "claim": {"type": "string", "description": "The specific claim this citation relates to."},
    "relation": {"type": "string", "enum": ["supports", "contradicts", "context", "uncertain"],
                 "description": "Relation to the hypothesis. Use uncertain if applicability is unverified."},
    "support_level": {"type": "string", "enum": ["direct", "indirect", "background", "unknown"]},
    "scope": {"type": "string", "description": "Source study conditions: DNA/RNA, organism, pore chemistry and basecaller when reported. State unknowns."},
}
for _tool, _field in ((RECORD_HYPOTHESIS_TOOL, "citations"), (RECORD_REVIEW_TOOL, "evidence")):
    _tool["input_schema"]["properties"][_field]["items"]["properties"].update(_EVIDENCE_FIELDS)
