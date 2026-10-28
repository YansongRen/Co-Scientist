"""End-to-end checks on the OpenAI-compatible provider seam.

`test_provider.py` exercises `_build_openai_request` directly, passing the
tool-call replay allowlist in as an argument. That leaves the seam that actually
matters untested: whether `get_provider("gemini")` threads `provider_name` far
enough that a real outbound payload carries Gemini's `extra_content` thought
signature — and, just as importantly, that no other OpenAI-compatible preset
starts sending a field a strict endpoint would reject.

These drive the whole path (`get_provider` → `call` → the SDK boundary) against
a stubbed `AsyncOpenAI`, asserting on the request dict as sent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from co_scientist.llm.anthropic_client import AgentCallSpec, CachedBlock, CallContext
from co_scientist.llm.budgets import TokenBudget
from co_scientist.llm.provider import get_provider
from co_scientist.llm.routing import ModelRoute
from co_scientist.models import ResearchPlan, Session


def _route(model: str, agent: str = "generation", mode: str = "", thinking: int = 0):
    return ModelRoute(
        agent=agent, mode=mode, model=model, thinking_tokens=thinking,
    )


def _resp():
    msg = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        model="m",
    )


def _continuation_spec(model: str, agent="generation", mode=""):
    """A spec mid-tool-loop: assistant tool_use carrying a thought signature."""
    return AgentCallSpec(
        route=_route(model, agent=agent, mode=mode),
        system_blocks=[CachedBlock("sys")],
        user_blocks=[CachedBlock("find papers")],
        extra_messages=[
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "pubmed_search",
                    "input": {"q": "x"},
                    "provider_fields": {
                        "extra_content": {"google": {"thought_signature": "SIG"}},
                        "index": 0,
                    },
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result", "tool_use_id": "call_1", "content": "r",
                }],
            },
        ],
        max_output_tokens=256,
    )


async def _ensure_session(conn) -> None:
    """Insert the session row the transcript foreign key needs, once."""
    from co_scientist.storage.repos import sessions as repo

    if await repo.fetch(conn, "ses_x") is not None:
        return
    await repo.insert(conn, Session(
        id="ses_x", created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        status="running", research_goal="g", research_plan=ResearchPlan(objective="x"),
        config_snapshot={}, budget_tokens=100_000, budget_usd=10.0,
    ))


async def _capture(cfg, conn, provider: str, spec) -> dict:
    """Run one call through get_provider(provider) and return the sent request."""
    cfg.llm.provider = provider
    await _ensure_session(conn)
    budget = TokenBudget(cfg=cfg, budget_tokens=100_000, budget_usd=10.0)

    with patch("openai.AsyncOpenAI") as mock_sdk:
        inst = mock_sdk.return_value
        inst.chat = MagicMock()
        inst.chat.completions = MagicMock()
        create = AsyncMock(return_value=_resp())
        inst.chat.completions.create = create
        client = get_provider(cfg, db=conn, budget=budget)
        ctx = CallContext(
            session_id="ses_x", task_id=None, agent="generation", action="t",
        )
        await client.call(spec, ctx)
    return create.await_args.kwargs


@pytest.mark.asyncio
async def test_gemini_provider_replays_thought_signature_end_to_end(tmp_cfg, conn):
    cfg = tmp_cfg
    cfg.secrets.GEMINI_API_KEY = "fake"
    sent = await _capture(
        cfg, conn, "gemini", _continuation_spec("gemini-3-flash"),
    )
    tool_call = sent["messages"][-2]["tool_calls"][0]
    assert tool_call["extra_content"] == {"google": {"thought_signature": "SIG"}}
    # response-only noise is retained internally but never sent
    assert "index" not in tool_call
    assert tool_call["function"]["name"] == "pubmed_search"


@pytest.mark.asyncio
async def test_google_alias_also_replays(tmp_cfg, conn):
    cfg = tmp_cfg
    cfg.secrets.GEMINI_API_KEY = "fake"
    sent = await _capture(cfg, conn, "google", _continuation_spec("gemini-3-flash"))
    assert "extra_content" in sent["messages"][-2]["tool_calls"][0]


@pytest.mark.asyncio
async def test_openrouter_does_not_leak_extra_content(tmp_cfg, conn):
    """OpenRouter is OpenAI-compat too — must stay strict."""
    cfg = tmp_cfg
    cfg.secrets.OPENROUTER_API_KEY = "fake"
    sent = await _capture(
        cfg, conn, "openrouter", _continuation_spec("google/gemini-3-flash"),
    )
    tool_call = sent["messages"][-2]["tool_calls"][0]
    assert set(tool_call) == {"id", "type", "function"}
    assert "reasoning_effort" not in sent


@pytest.mark.asyncio
async def test_plain_openai_stays_strict(tmp_cfg, conn):
    cfg = tmp_cfg
    cfg.secrets.OPENAI_API_KEY = "sk-fake"
    sent = await _capture(cfg, conn, "openai", _continuation_spec("gpt-5"))
    assert set(sent["messages"][-2]["tool_calls"][0]) == {"id", "type", "function"}
    assert "extra_body" not in sent


@pytest.mark.asyncio
async def test_compatible_provider_forwards_configured_body_extensions(tmp_cfg, conn):
    cfg = tmp_cfg
    cfg.secrets.OPENAI_API_KEY = "fake"
    cfg.llm.openai.base_url = "https://api.deepseek.com"
    cfg.llm.openai.extra_body = {"thinking": {"type": "disabled"}}
    spec = _continuation_spec("deepseek-flash")
    spec.tool_choice = {"type": "tool", "name": "record_hypothesis"}
    sent = await _capture(cfg, conn, "openai_compatible", spec)
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert sent["tool_choice"] == {
        "type": "function", "function": {"name": "record_hypothesis"},
    }


@pytest.mark.asyncio
async def test_gemini_thinking_level_global_and_per_mode(tmp_cfg, conn):
    cfg = tmp_cfg
    cfg.secrets.GEMINI_API_KEY = "fake"

    # default -> omitted entirely
    sent = await _capture(cfg, conn, "gemini", _continuation_spec("gemini-3-flash"))
    assert "reasoning_effort" not in sent

    # global level applies
    cfg.llm.gemini.thinking_level = "medium"
    sent = await _capture(cfg, conn, "gemini", _continuation_spec("gemini-3-flash"))
    assert sent["reasoning_effort"] == "medium"

    # per-mode override wins, keyed "agent.mode"
    cfg.llm.gemini.thinking_by_mode = {"metareview.final": "high"}
    sent = await _capture(
        cfg, conn, "gemini",
        _continuation_spec("gemini-3-flash", agent="metareview", mode="final"),
    )
    assert sent["reasoning_effort"] == "high"

    # a mode with no override falls back to the global level
    sent = await _capture(
        cfg, conn, "gemini",
        _continuation_spec("gemini-3-flash", agent="generation", mode="literature"),
    )
    assert sent["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_gemini_ignores_legacy_thinking_token_budget(tmp_cfg, conn):
    """With thinking_level=default, a numeric budget must not leak to Gemini."""
    cfg = tmp_cfg
    cfg.secrets.GEMINI_API_KEY = "fake"
    spec = _continuation_spec("gemini-3-flash")
    spec.route.thinking_tokens = 8000
    sent = await _capture(cfg, conn, "gemini", spec)
    assert "reasoning_effort" not in sent
