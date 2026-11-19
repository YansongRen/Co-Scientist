"""Tests for model routing + downgrade chain + cost estimation."""

from __future__ import annotations

from co_scientist.config import Config
from co_scientist.llm.routing import (
    NEVER_DEGRADE,
    estimate_cost_usd,
    route,
    thinking_budget_for,
)


def test_default_routes_use_opus_for_heavy_modes() -> None:
    cfg = Config()
    assert route(cfg, "generation", "literature").model == cfg.models.generation
    assert route(cfg, "reflection", "verification").model == cfg.models.reflection
    assert route(cfg, "metareview", "final").model == cfg.models.metareview_final


def test_thinking_only_on_opus() -> None:
    """Matched by substring so the CLI alias "opus" counts alongside
    "claude-opus-5"."""
    cfg = Config()
    r = route(cfg, "reflection", "verification")
    if "opus" in r.model.lower():
        assert r.thinking_tokens == cfg.thinking.reflection_verification
    else:
        assert r.thinking_tokens == 0


def test_thinking_is_zero_for_non_opus_routes() -> None:
    cfg = Config()
    assert "opus" not in cfg.models.ranking_pairwise.lower()
    assert route(cfg, "ranking", "pairwise").thinking_tokens == 0


def test_degrade_walks_chain_once() -> None:
    cfg = Config()
    r1 = route(cfg, "generation", "literature", degraded=False)
    r2 = route(cfg, "generation", "literature", degraded=True)
    # Should be different unless never-degrade
    assert "generation.literature" not in NEVER_DEGRADE
    assert r1.model != r2.model


def test_never_degrade_modes_stay_put() -> None:
    cfg = Config()
    r1 = route(cfg, "reflection", "verification", degraded=False)
    r2 = route(cfg, "reflection", "verification", degraded=True)
    assert r1.model == r2.model


def test_thinking_budget_lookup() -> None:
    cfg = Config()
    assert thinking_budget_for(cfg, "reflection.verification") == cfg.thinking.reflection_verification
    assert thinking_budget_for(cfg, "ranking.pairwise") == cfg.thinking.ranking_pairwise
    assert thinking_budget_for(cfg, "made_up.mode") == 0


def test_cache_reads_are_cheaper_than_uncached_input() -> None:
    """Same total 10k-token context: all-uncached is more expensive than
    mostly-cached (2k uncached + 8k cache reads)."""
    all_uncached = estimate_cost_usd(
        model="claude-opus-4-7", input_tokens=10_000, output_tokens=1_000
    )
    mostly_cached = estimate_cost_usd(
        model="claude-opus-4-7",
        input_tokens=2_000, output_tokens=1_000,
        cache_read=8_000, cache_write=0,
    )
    assert mostly_cached < all_uncached


def test_unknown_flash_model_prices_as_flash_not_sonnet() -> None:
    """Brand-new gemini-3-flash-preview must NOT be priced at the conservative
    sonnet-class fallback (otherwise the pre-flight estimator over-reports by
    10x and the budget admission rejects work that should be affordable).
    """
    flash_cost = estimate_cost_usd(
        model="google/gemini-3-flash-preview",
        input_tokens=1_000_000, output_tokens=1_000_000,
    )
    # flash-tier output is ~$2.5/M, so 1M+1M tokens ≈ $2.80.
    # sonnet fallback would be ~$18.
    assert flash_cost < 5.0


def test_unknown_mini_model_uses_mini_tier_pricing() -> None:
    cost = estimate_cost_usd(
        model="some-provider/gpt-7-mini-preview",
        input_tokens=1_000_000, output_tokens=1_000_000,
    )
    # mini tier: 1.1 input + 4.4 output per million ≈ $5.5
    # gpt-5 tier would be ~$25, sonnet ~$18
    assert cost < 10.0


def test_unknown_opus_class_model_uses_opus_pricing() -> None:
    """Family hints map upward as well: an unknown opus-named model should
    NOT silently price as a sonnet (and risk underbudgeting)."""
    opus = estimate_cost_usd(
        model="anthropic/claude-99-opus-experimental",
        input_tokens=1_000_000, output_tokens=1_000_000,
    )
    sonnet = estimate_cost_usd(
        model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000,
    )
    # opus tier: 5 + 25 = $30/M+M, versus sonnet's 3 + 15 = $18/M+M.
    assert opus == 30.0
    assert opus > sonnet


def test_known_model_takes_precedence_over_family_hint() -> None:
    """`gemini-3-flash-preview` is in the table; the family hint should not
    override the explicit entry."""
    flash_known = estimate_cost_usd(
        model="gemini-3-flash-preview",
        input_tokens=1_000_000, output_tokens=1_000_000,
    )
    assert 2.5 < flash_known < 3.5


def test_completely_unknown_model_uses_conservative_fallback() -> None:
    """No family hint match → fall back to sonnet-class so a misconfigured
    route doesn't accidentally run unbounded on a cheap fallback."""
    cost = estimate_cost_usd(
        model="totally-novel-vendor/unrecognized-model-7",
        input_tokens=1_000_000, output_tokens=1_000_000,
    )
    # Sonnet-class fallback: 3 + 15 = $18/M+M
    assert 15.0 < cost < 25.0


def test_degrade_works_for_every_naming_style_we_ship() -> None:
    """`[models]` holds full ids, CLI aliases, or vendor-prefixed ids depending
    on the configured backend. An exact-match chain in one style silently made
    degradation a no-op for the others."""
    from co_scientist.llm.routing import degrade_model

    assert degrade_model("claude-opus-4-7") == "claude-sonnet-4-6"
    assert degrade_model("opus") == "sonnet"
    assert degrade_model("anthropic/claude-opus-4-7") == "anthropic/claude-sonnet-4-6"


def test_degrade_leaves_the_bottom_of_the_chain_and_unknown_models_alone() -> None:
    from co_scientist.llm.routing import degrade_model

    assert degrade_model("haiku") == "haiku"
    assert degrade_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
    # Guessing a cheaper id for an unrecognized model risks naming one the
    # endpoint rejects; costing more is the safer failure.
    assert degrade_model("gemini-3-pro") == "gemini-3-pro"


def test_degrade_still_respects_never_degrade_modes() -> None:
    cfg = Config()
    cfg.models.reflection = "opus"
    r = route(cfg, "reflection", "verification", degraded=True)
    assert r.model == "opus"


def test_openai_compatible_classifier_falls_back_to_parse_goal() -> None:
    cfg = Config()
    cfg.llm.provider = "openai_compatible"
    cfg.models.parse_goal = "deepseek-chat"
    cfg.models.classifier = "anthropic/claude-haiku-4.5"
    assert route(cfg, "classifier").model == "deepseek-chat"
