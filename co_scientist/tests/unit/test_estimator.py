"""Tests for the pre-flight cost estimator."""

from __future__ import annotations

from co_scientist.config import Config
from co_scientist.llm.estimator import estimate


def test_estimate_emits_warning_when_budget_too_low() -> None:
    cfg = Config()
    cfg.run.budget_usd = 1.0
    est = estimate(cfg)
    assert est.total_usd > cfg.run.budget_usd
    assert est.warning is not None and "exceeds" in est.warning


def test_estimate_no_warning_when_budget_generous() -> None:
    cfg = Config()
    cfg.run.budget_usd = 9999.0
    est = estimate(cfg)
    assert est.warning is None


def test_estimate_rows_include_all_phases() -> None:
    cfg = Config()
    est = estimate(cfg)
    labels = {r.label for r in est.rows}
    assert {
        "parse_goal", "generation", "reflection.full",
        "ranking_pairwise", "metareview.final",
    } <= labels


def test_estimate_scales_with_max_ideas() -> None:
    cfg = Config()
    small = estimate(cfg, max_ideas=10, max_matches_per_idea=4)
    big = estimate(cfg, max_ideas=100, max_matches_per_idea=12)
    assert big.total_usd > small.total_usd * 5


def test_estimate_periodic_meta_follows_config() -> None:
    cfg = Config()
    cfg.metareview.feedback_every_matches = 10
    est = estimate(cfg, max_ideas=20, max_matches_per_idea=10)
    meta = next(r for r in est.rows if r.label == "metareview.system")
    assert meta.n_calls == max(1, (20 * 10 // 2) // 10)
    cfg.metareview.feedback_every_matches = 0
    silent = estimate(cfg, max_ideas=20, max_matches_per_idea=10)
    assert next(r for r in silent.rows if r.label == "metareview.system").n_calls == 0
