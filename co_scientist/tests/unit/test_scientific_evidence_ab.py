from co_scientist.evals.scientific_evidence_ab import compare_case


def test_ab_comparison_is_deterministic_and_flags_unsupported_activity_claims():
    result = compare_case({
        "case_id": "kemp-1",
        "baseline": {
            "reflection": {"model": "offline", "output": "neutral"},
        },
        "evidence": {
            "reflection": {
                "model": "offline",
                "output": "sci_abc123 metric=0.71; this proves catalytic activity",
                "evidence_ids": ["sci_abc123"],
            },
        },
    })
    row = result["comparisons"][0]
    assert row["output_changed"] is True
    assert row["evidence_references"] == ["sci_abc123"]
    assert row["unsupported_activity_claim_evidence"] is True
    assert "does not establish scientific performance" in row["interpretation"]
