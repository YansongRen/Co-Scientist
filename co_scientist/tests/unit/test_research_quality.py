"""Scientific provenance and bounded scheduling regressions; no external calls."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from co_scientist.agents.base import AgentDeps
from co_scientist.agents.generation import GenerationAgent
from co_scientist.agents.supervisor import Supervisor
from co_scientist.models import Hypothesis, ResearchPlan, Session, Task, TournamentMatch
from co_scientist.orchestrator.research_policy import adaptive_maturity, eligible_pairs
from co_scientist.safety.evidence import (
    NOVELTY_POLICY,
    annotate_record,
    audit_evidence,
    collect_sources,
)
from co_scientist.storage.artifacts import read_json
from co_scientist.storage.repos import hypotheses, sessions, tasks, tournaments
from co_scientist.web.app import create_app

URL = "https://example.org/paper"
QUOTE = "RNA sequencing accuracy depends on the experimental conditions."


def test_empty_search_does_not_establish_novelty_and_failure_is_distinct():
    result = SimpleNamespace(source_observations=[], tool_calls=[
        {"name": "pubmed_search", "args": {"query": "A"}, "result_count": 0, "is_error": False},
        {"name": "web_search", "args": {"query": "B"}, "result_count": 0, "is_error": True},
    ])
    record = {"statement": "A hypothesis", "citations": []}
    annotate_record(record, result)
    assert record["novelty_assessment"]["status"] == "unverified"
    assert [q["status"] for q in record["novelty_assessment"]["searches"]] == ["empty", "failed"]
    assert "do not establish novelty" in NOVELTY_POLICY


def test_quote_matching_is_separate_from_scientific_support_and_scope():
    sources = collect_sources({"results": [{"url": URL, "title": "Direct RNA sequencing",
        "abstract": QUOTE}]}, tool="europe_pmc_search")
    cards = audit_evidence([{"url": URL, "claim": "DNA motor error", "excerpt": QUOTE,
                            "relation": "supports", "support_level": "direct"}], sources, molecule="DNA")
    card = cards[0]
    assert card["quote_status"] == "quote_found"
    assert card["retrieval_level"] == "abstract"
    assert card["semantic_status"] == "needs_human_review"
    assert card["source_molecule"] == "RNA"
    assert card["scope_warning"]


def test_full_quote_must_match_and_failed_fetch_is_not_evidence():
    quote = "A" * 210 + "this suffix was invented"
    sources = collect_sources({"url": URL, "text": "A" * 210}, tool="web_fetch")
    assert audit_evidence([{"url": URL, "excerpt": quote}], sources)[0]["quote_status"] == "quote_not_found"
    assert audit_evidence([{"url": URL, "excerpt": "DNA"}], sources)[0]["quote_status"] == "quote_too_short"
    assert collect_sources({"url": URL, "text": QUOTE}, tool="web_fetch", failed=True) == []
    assert audit_evidence([{"url": URL, "excerpt": QUOTE}], [])[0]["quote_status"] == "not_checked"
    assert audit_evidence([{"url": "javascript:alert(1)"}], []) == []


async def seed(conn, *, matches=3):
    now = datetime.now(UTC)
    session = Session(id="quality-session", created_at=now, updated_at=now, status="running",
        research_goal="Study nanopore DNA sequencing", research_plan=ResearchPlan(objective="Test"),
        config_snapshot={}, budget_tokens=10000, budget_usd=1)
    await sessions.insert(conn, session)
    pool = []
    for name in ("a", "b"):
        h = Hypothesis(id=name, session_id=session.id, created_at=now, created_by="generation",
            strategy="literature", title=name, summary=name, full_text=name,
            artifact_path=f"artifacts/{name}.json", elo=1200, matches_played=matches, state="in_tournament")
        await hypotheses.insert(conn, h)
        pool.append(h)
    for index in range(matches):
        await tournaments.insert_match(conn, TournamentMatch(
            id=f"match-{index}", session_id=session.id, created_at=now,
            hyp_a="a" if index % 2 else "b", hyp_b="b" if index % 2 else "a",
            mode="pairwise", winner="a", elo_a_before=1200, elo_b_before=1200))
    return session, pool


@pytest.mark.asyncio
async def test_two_hypotheses_get_feedback_and_evolution_without_repeated_ranking(tmp_cfg, conn):
    session, pool = await seed(conn)
    tmp_cfg.run.research_mode = "lightweight"
    tmp_cfg.run.max_ideas = 6
    assert tmp_cfg.evolution.min_mature == 20
    assert adaptive_maturity(tmp_cfg, pool)
    assert await eligible_pairs(conn, session.id, pool, pair_cap=3) == []
    supervisor = Supervisor(tmp_cfg)
    assert await supervisor._decide_next_steps(conn, session) == 1
    async with conn.execute("SELECT id,action FROM tasks ORDER BY rowid") as cur:
        first = await cur.fetchone()
    assert first["action"] == "GenerateSystemFeedback"
    assert await supervisor._decide_next_steps(conn, session) == 0  # feedback still pending
    await tasks.complete(conn, first["id"])
    assert await supervisor._decide_next_steps(conn, session) == 1
    async with conn.execute("SELECT id,action FROM tasks ORDER BY rowid DESC LIMIT 1") as cur:
        second = await cur.fetchone()
    assert second["action"] == "EvolveTopHypotheses"
    await tasks.complete(conn, second["id"])
    # A duplicate/no-output evolution must not be enqueued forever.
    assert await supervisor._decide_next_steps(conn, session) == 0


@pytest.mark.asyncio
async def test_saturated_pairs_do_not_block_new_candidate_comparisons(tmp_cfg, conn):
    session, pool = await seed(conn)
    new = pool[0].model_copy(update={"id": "c", "matches_played": 0})
    pairs = await eligible_pairs(conn, session.id, [*pool, new], pair_cap=3)
    assert len(pairs) == 2
    assert all("c" in {a.id, b.id} for a, b in pairs)


@pytest.mark.asyncio
async def test_generation_persists_audit_and_safe_novelty_prompt(tmp_cfg, conn, monkeypatch):
    now = datetime.now(UTC)
    await sessions.insert(conn, Session(id="generation-test", created_at=now, updated_at=now,
        status="running", research_goal="nanopore DNA sequencing",
        research_plan=ResearchPlan(objective="Test", target_molecule="DNA"), config_snapshot={},
        budget_tokens=1000, budget_usd=1))
    record = {"title": "Test", "statement": "A test hypothesis", "mechanism": "Test",
              "novelty_argument": "Provisional", "citations": [{"url": URL,
              "title": "RNA sequencing", "excerpt": QUOTE}]}
    response = SimpleNamespace(raw=SimpleNamespace(content=[SimpleNamespace(
        type="tool_use", name="record_hypothesis", input=record)]))
    result = SimpleNamespace(response=response, iterations=1, seen_urls={URL}, tool_calls=[],
        source_observations=collect_sources({"url": URL, "text": QUOTE}, tool="web_fetch"))
    run = AsyncMock(return_value=result)
    monkeypatch.setattr("co_scientist.agents.generation.run_tool_loop", run)
    registry = MagicMock()
    registry.anthropic_tools_for.return_value = []
    agent = GenerationAgent(AgentDeps(tmp_cfg, conn, MagicMock(), registry))
    monkeypatch.setattr(agent, "_dedup_query", AsyncMock(return_value=(None, None)))
    outcome = await agent.execute(Task(id="gen-task", session_id="generation-test", created_at=now,
                                      agent="generation", action="CreateInitialHypotheses"))
    spec = run.call_args.kwargs["spec"]
    assert "empty searches CONFIRM novelty" not in spec.user_blocks[0].text
    h = await hypotheses.fetch(conn, outcome.hypothesis_ids[0])
    artifact = await read_json(tmp_cfg, h.artifact_path)
    assert artifact["record"]["novelty_assessment"]["status"] == "unverified"
    assert artifact["record"]["evidence_audit"][0]["quote_status"] == "quote_found"
    assert artifact["record"]["evidence_audit"][0]["scope_warning"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_cfg)), base_url="http://test") as client:
        data = (await client.get("/api/studio/sessions/generation-test")).json()
    assert data["evidence"]["summary"]["quotes_matched"] == 1
    assert data["evidence"]["summary"]["scope_warnings"] == 1


@pytest.mark.asyncio
async def test_launch_mode_is_bounded_and_validation_does_not_start_research(tmp_cfg, monkeypatch):
    import asyncio

    launched = []
    ready = asyncio.Event()

    async def fake_run(self, **kwargs):
        launched.append((self.cfg, kwargs))
        ready.set()
        return "local-only"

    monkeypatch.setattr(Supervisor, "run_session", fake_run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_cfg)), base_url="http://test") as client:
        for invalid in ({"n_initial": 1}, {"n_initial": 7}, {"budget_usd": -1}, {"research_mode": "bad"}):
            response = await client.post("/sessions/new", data={"goal": "Test", **invalid})
            assert response.status_code == 422
        assert launched == []
        response = await client.post("/sessions/new", data={"goal": "Test", "n_initial": 2,
            "research_mode": "lightweight", "target_molecule": "DNA", "budget_usd": 1})
        assert response.status_code == 303
        await asyncio.wait_for(ready.wait(), 1)
    cfg, kwargs = launched[0]
    assert cfg.run.concurrency == 1
    assert cfg.run.max_ideas == 6
    assert cfg.run.target_molecule == "DNA"
    assert "Research scope: DNA" in kwargs["preferences_text"]
    assert tmp_cfg.run.research_mode == "standard"  # per-run settings cannot leak


def test_metareview_source_linkage_is_not_automatically_labeled_support():
    from co_scientist.agents.metareview import MetaReviewAgent
    from co_scientist.models import CitedPaper

    h = SimpleNamespace(id="h", citations=[CitedPaper(url=URL, title="Paper")])
    block = MetaReviewAgent._source_block([h], [])
    assert "cited by hypotheses" in block
    assert "(supports:" not in block


@pytest.mark.asyncio
async def test_tool_loop_retains_only_returned_source_text(tmp_cfg):
    from co_scientist.llm.routing import ModelRoute
    from co_scientist.llm.tool_loop import run_tool_loop
    from co_scientist.llm.types import AgentCallSpec, CallContext, LLMResponse
    from co_scientist.tools.base import ToolResult
    from co_scientist.tools.registry import ToolRegistry

    class Search:
        name = "local_search"

        async def call(self, args, ctx):
            return ToolResult(content={"results": [{"url": URL, "abstract": QUOTE}]})

    registry = ToolRegistry(tmp_cfg)
    registry._register(Search())

    def response(name):
        raw = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
            type="tool_use", id=name, name=name, input={"query": "RNA"})])
        return LLMResponse(raw=raw, transcript_id="local", cost_usd=0, input_tokens=0,
                           output_tokens=0, cache_read=0, cache_write=0)

    client = SimpleNamespace(runs_own_loop=False, call=AsyncMock(side_effect=[
        response("local_search"), response("record_hypothesis")]))
    result = await run_tool_loop(client,
        spec=AgentCallSpec(route=ModelRoute(agent="generation", mode="literature", model="fake")),
        ctx=CallContext("local", "task", "generation", "test"), registry=registry, max_iters=2)
    assert result.tool_calls[0]["result_count"] == 1
    assert result.source_observations[0]["text"] == QUOTE
    assert result.source_observations[0]["level"] == "abstract"
