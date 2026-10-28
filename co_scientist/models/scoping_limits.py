"""User-editable ceilings, bounded by the server's deployment policy."""
from pydantic import BaseModel, Field


class ScopingLimits(BaseModel):
    budget_usd: float = Field(1, gt=0, le=25, allow_inf_nan=False)
    budget_tokens: int = Field(60000, ge=1000, le=10000000)
    wall_clock_seconds: int = Field(600, ge=1, le=7200)


def effective_limits(cfg, raw=None):
    limits = ScopingLimits.model_validate(raw or {"budget_usd": cfg.scoping.budget_usd,
        "budget_tokens": cfg.scoping.max_tokens, "wall_clock_seconds": 600})
    return limits.model_copy(update={"budget_usd": min(limits.budget_usd, cfg.scoping.max_budget_usd),
                                     "budget_tokens": min(limits.budget_tokens, cfg.scoping.max_budget_tokens),
                                     "wall_clock_seconds": min(limits.wall_clock_seconds, cfg.scoping.max_run_seconds)})


def check_limits(cfg, limits):
    if limits.budget_usd > cfg.scoping.max_budget_usd or limits.budget_tokens > cfg.scoping.max_budget_tokens or limits.wall_clock_seconds > cfg.scoping.max_run_seconds:
        raise ValueError("Requested limits exceed server ceilings")
    return limits
