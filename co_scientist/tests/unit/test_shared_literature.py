"""Shared literature is one session-model pass, then authors stop searching."""
from datetime import UTC, datetime, timedelta

import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.literature.shared import (
    WATERMARK,
    gather_shared_literature,
    generation_context,
    normalize_papers,
    render_brief_markdown,
)
from co_scientist.llm.types import LLMResponse, SynthMessage, ToolUseBlock
from co_scientist.models import ResearchPlan, Session
from co_scientist.storage.repos import sessions as sess_repo
from co_scientist.tools.registry import ToolRegistry


def test_reading_notes_keep_only_seen_urls_and_real_depth():
    papers = normalize_papers(
        [
            {"title": "Seen", "url": "https://example.test/a", "reading_depth": "full_text",
             "question": "Q", "results": "R"},
            {"title": "Invented", "url": "https://example.test/missing", "reading_depth": "full_text",
             "question": "Q", "results": "R"},
        ],
        [{"url": "https://example.test/a", "level": "abstract", "title": "Seen"}],
    )
    assert [paper["url"] for paper in papers] == ["https://example.test/a"]
    assert papers[0]["reading_depth"] == "abstract_only"
    text = render_brief_markdown({"overview": "概述", "papers": papers, "status": "ready"})
    assert "norman-paper-reading" in text
    assert text.strip().endswith(WATERMARK)


def test_generation_falls_back_when_shared_pass_fails_or_is_empty():
    assert generation_context(None)[1] is True
    failed_text, failed_search = generation_context(
        {"status": "failed", "papers": [], "error_type": "TimeoutError"},
    )
    assert failed_search is True
    assert "TimeoutError" in failed_text
    assert "Search yourself" in failed_text
    empty_ready_text, empty_search = generation_context({"status": "ready", "papers": []})
    assert empty_search is True
    assert "did not complete" in empty_ready_text


def test_ready_brief_is_exclusive_except_feedback_driven():
    ready = {
        "status": "ready", "overview": "共享",
        "papers": [{"title": "A", "url": "https://example.test/a", "reading_depth": "abstract_only",
                    "question": "问题", "design": "", "results": "结果", "gaps": "", "next_step": "",
                    "reading_priority": ""}],
    }
    ready_text, ready_search = generation_context(ready, strategy="literature")
    assert ready_search is False
    assert "https://example.test/a" in ready_text
    assert WATERMARK in ready_text
    _, feedback_search = generation_context(ready, strategy="feedback_driven")
    assert feedback_search is True
    _, open_search = generation_context(ready, strategy="literature", exclusive=False)
    assert open_search is True


class _SessionModel:
    def __init__(self):
        self.routes = []
        self.specs = []

    async def call(self, spec, ctx, **kwargs):
        self.routes.append(spec.route)
        self.specs.append(spec)
        message = SynthMessage(
            stop_reason="tool_use",
            content=[ToolUseBlock(
                id="lit", name="record_shared_literature",
                input={"overview": "共享概述", "papers": [{
                    "title": "未见过", "url": "https://example.test/invented",
                    "reading_depth": "full_text", "question": "问题", "results": "结果",
                }]},
            )],
        )
        return LLMResponse(
            raw=message, transcript_id="", cost_usd=0, input_tokens=1, output_tokens=1,
            cache_read=0, cache_write=0,
        )


@pytest.mark.asyncio
async def test_shared_pass_uses_the_session_model_not_a_generation_author(tmp_cfg, conn):
    tmp_cfg.models.parse_goal = "deepseek-flash"
    tmp_cfg.models.generation = "deepseek-flash"
    tmp_cfg.generation.authors = ["kimi", "glm", "session"]
    now = datetime.now(UTC)
    session = Session(
        id="ses_lit", created_at=now, updated_at=now, status="running",
        research_goal="问题", research_plan=ResearchPlan(objective="问题"),
        config_snapshot={}, budget_tokens=1000, budget_usd=1,
    )
    await sess_repo.insert(conn, session)
    llm = _SessionModel()
    deps = AgentDeps(tmp_cfg, conn, llm, ToolRegistry(tmp_cfg).discover())
    record = await gather_shared_literature(deps, session)
    assert record["status"] == "ready"
    assert record["model"] == "deepseek-flash"
    assert record["endpoint"] == ""
    assert record["papers"] == []
    assert record["skill"] == "norman-paper-reading"
    assert record["skill_mode"] == "quick"
    assert llm.routes[0].model == "deepseek-flash"
    assert llm.routes[0].endpoint == ""
    tool_names = {tool["name"] for tool in llm.specs[0].tools}
    assert "web_search" not in tool_names
    assert "pubmed_search" in tool_names
    saved = await gather_shared_literature(deps, session)
    assert saved["model"] == "deepseek-flash"
    assert len(llm.routes) == 1


@pytest.mark.asyncio
async def test_shared_pass_skips_when_wall_clock_is_almost_gone(tmp_cfg, conn):
    now = datetime.now(UTC)
    session = Session(
        id="ses_lit_deadline", created_at=now, updated_at=now, status="running",
        research_goal="问题", research_plan=ResearchPlan(objective="问题"),
        config_snapshot={}, budget_tokens=0, budget_usd=1,
        wall_deadline=now + timedelta(seconds=30),
    )
    await sess_repo.insert(conn, session)
    llm = _SessionModel()
    deps = AgentDeps(tmp_cfg, conn, llm, ToolRegistry(tmp_cfg).discover())
    record = await gather_shared_literature(deps, session)
    assert record["status"] == "failed"
    assert record["error_type"] == "wall_clock"
    assert llm.specs == []
