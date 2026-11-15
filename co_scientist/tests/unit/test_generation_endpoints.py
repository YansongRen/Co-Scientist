"""Generation can leave the session provider for a named endpoint."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from co_scientist.config import EndpointCfg
from co_scientist.llm.anthropic_client import AgentCallSpec, CachedBlock, CallContext
from co_scientist.llm.budgets import TokenBudget
from co_scientist.llm.provider import get_provider
from co_scientist.llm.routing import ModelRoute, generation_author, route
from co_scientist.models import ResearchPlan, Session


def test_generation_endpoint_overrides_only_that_role(tmp_cfg):
    tmp_cfg.models.generation = "deepseek-flash"
    tmp_cfg.models.reflection = "deepseek-flash"
    tmp_cfg.generation.endpoints = {
        "literature": "kimi",
        "debate": "kimi",
        "skeptic": "glm",
        "feedback_driven": "glm",
    }
    tmp_cfg.llm.endpoints = {
        "kimi": EndpointCfg(base_url="https://api.moonshot.cn/v1", model="moonshot-v1-128k", api_key_env="KIMI_API_KEY"),
        "glm": EndpointCfg(base_url="https://open.bigmodel.cn/api/paas/v4/", model="glm-4.5", api_key_env="GLM_API_KEY"),
    }
    literature = route(tmp_cfg, "generation", "literature")
    skeptic = route(tmp_cfg, "generation", "skeptic")
    review = route(tmp_cfg, "reflection", "full")
    assert literature.model == "moonshot-v1-128k" and literature.endpoint == "kimi"
    assert skeptic.model == "glm-4.5" and skeptic.endpoint == "glm"
    assert review.model == "deepseek-flash" and review.endpoint == ""


def test_unconfigured_generation_stays_on_the_session_model(tmp_cfg):
    got = route(tmp_cfg, "generation", "literature")
    assert got.model == tmp_cfg.models.generation
    assert got.endpoint == ""


def test_both_authors_write_every_generation_strategy(tmp_cfg):
    tmp_cfg.models.generation = "deepseek-flash"
    tmp_cfg.models.reflection = "deepseek-flash"
    tmp_cfg.generation.authors = ["kimi", "glm", "session"]
    tmp_cfg.llm.endpoints = {
        "kimi": EndpointCfg(base_url="https://api.moonshot.cn/v1", model="kimi-k3.1-pro", api_key_env="KIMI_API_KEY"),
        "glm": EndpointCfg(base_url="https://open.bigmodel.cn/api/paas/v4/", model="glm-5.3", api_key_env="GLM_API_KEY"),
    }
    assert generation_author(tmp_cfg, batch_index=0) == "kimi"
    assert generation_author(tmp_cfg, batch_index=1) == "glm"
    assert generation_author(tmp_cfg, batch_index=2) == "session"
    assert generation_author(tmp_cfg, batch_index=3) == "kimi"
    for mode in ("literature", "debate", "assumption", "feedback_driven"):
        kimi = route(tmp_cfg, "generation", mode, endpoint="kimi")
        glm = route(tmp_cfg, "generation", mode, endpoint="glm")
        deepseek = route(tmp_cfg, "generation", mode, endpoint="session")
        assert (kimi.model, kimi.endpoint) == ("kimi-k3.1-pro", "kimi")
        assert (glm.model, glm.endpoint) == ("glm-5.3", "glm")
        assert (deepseek.model, deepseek.endpoint) == ("deepseek-flash", "")
    review = route(tmp_cfg, "reflection", "full")
    assert review.model == "deepseek-flash" and review.endpoint == ""


def _resp():
    msg = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        model="m",
    )


@pytest.mark.asyncio
async def test_named_generation_call_uses_its_own_key_and_skips_session_body(tmp_cfg, conn):
    from co_scientist.storage.repos import sessions as repo

    tmp_cfg.llm.provider = "openai_compatible"
    tmp_cfg.llm.openai.base_url = "https://api.deepseek.com"
    tmp_cfg.llm.openai.extra_body = {"thinking": {"type": "disabled"}}
    tmp_cfg.secrets.OPENAI_API_KEY = "deepseek-secret"
    tmp_cfg.secrets.KIMI_API_KEY = "kimi-secret"
    tmp_cfg.generation.endpoints = {"literature": "kimi"}
    tmp_cfg.llm.endpoints = {
        "kimi": EndpointCfg(
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k3",
            api_key_env="KIMI_API_KEY",
        ),
    }
    await repo.insert(conn, Session(
        id="ses_x", created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        status="running", research_goal="g", research_plan=ResearchPlan(objective="x"),
        config_snapshot={}, budget_tokens=100_000, budget_usd=10.0,
    ))
    made: list[MagicMock] = []

    def factory(**kwargs):
        inst = MagicMock()
        inst.init_kwargs = kwargs
        inst.chat.completions.create = AsyncMock(return_value=_resp())
        made.append(inst)
        return inst

    with patch("openai.AsyncOpenAI", side_effect=factory):
        client = get_provider(tmp_cfg, db=conn, budget=TokenBudget(cfg=tmp_cfg, budget_tokens=100_000, budget_usd=10.0))
        spec = AgentCallSpec(
            route=ModelRoute(agent="generation", mode="generation.literature", model="kimi-k3", endpoint="kimi"),
            system_blocks=[CachedBlock("sys")],
            user_blocks=[CachedBlock("propose")],
            max_output_tokens=64,
        )
        ctx = CallContext(session_id="ses_x", task_id=None, agent="generation", action="t")
        await client.call(spec, ctx)
        review = AgentCallSpec(
            route=ModelRoute(agent="reflection", mode="reflection.full", model="deepseek-flash"),
            system_blocks=[CachedBlock("sys")],
            user_blocks=[CachedBlock("review")],
            max_output_tokens=64,
        )
        await client.call(review, CallContext(session_id="ses_x", task_id=None, agent="reflection", action="t"))

    assert made[0].init_kwargs["base_url"] == "https://api.deepseek.com"
    assert made[0].init_kwargs["api_key"] == "deepseek-secret"
    assert made[1].init_kwargs["base_url"] == "https://api.moonshot.cn/v1"
    assert made[1].init_kwargs["api_key"] == "kimi-secret"
    kimi_sent = made[1].chat.completions.create.await_args.kwargs
    assert kimi_sent["model"] == "kimi-k3"
    assert "extra_body" not in kimi_sent
    deepseek_sent = made[0].chat.completions.create.await_args.kwargs
    assert deepseek_sent["model"] == "deepseek-flash"
    assert deepseek_sent["extra_body"] == {"thinking": {"type": "disabled"}}
