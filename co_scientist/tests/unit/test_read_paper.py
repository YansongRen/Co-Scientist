"""Open-paper reading returns methods and results, and says when it cannot."""
from __future__ import annotations

import json

import pytest

from co_scientist.config import Config
from co_scientist.safety.evidence import audit_evidence, collect_sources
from co_scientist.tools.base import ToolCtx
from co_scientist.tools.read_paper import ReadPaperTool, jats_text, reading_packet
from co_scientist.tools.registry import ToolRegistry


METHODS = "The assay used a control cohort of forty samples."
RESULTS = "The treated group diverged from the control cohort."
XML = f"""
<article>
  <front><article-meta>
    <article-title>Sample study</article-title>
    <abstract><p>Short abstract only.</p></abstract>
  </article-meta></front>
  <body>
    <sec><title>Introduction</title><p>{"Intro " * 80}</p></sec>
    <sec><title>Methods</title><p>{METHODS}</p></sec>
    <sec><title>Results</title><p>{RESULTS}</p></sec>
  </body>
</article>
""".encode()


def test_reading_packet_keeps_methods_and_results_not_only_the_opening():
    title, text = jats_text(XML)
    packet = reading_packet(text)
    assert title == "Sample study"
    assert packet["reading_depth"] == "full_text"
    assert METHODS in packet["text"]
    assert RESULTS in packet["text"]
    assert "Intro Intro" not in packet["text"]


def test_unsectioned_long_text_includes_the_middle():
    marker = "MIDDLE-MARKER-UNIQUE"
    text = ("alpha " * 1600) + marker + (" omega" * 1600)
    packet = reading_packet(text)
    assert packet["reading_depth"] == "full_text"
    assert marker in packet["text"]


def test_short_abstract_is_not_called_full_text():
    packet = reading_packet("Abstract\nOnly the abstract was available.")
    assert packet["reading_depth"] == "abstract_only"


@pytest.mark.asyncio
async def test_pmc_read_returns_methods_and_matches_a_quote(monkeypatch):
    async def fake_fetch(self, url, params=None):
        assert url.endswith("/PMC9/fullTextXML")
        return XML, url, "application/xml"

    monkeypatch.setattr(ReadPaperTool, "_fetch", fake_fetch)
    tool = ReadPaperTool(Config())
    result = await tool.call({"pmcid": "PMC9"}, ToolCtx(cfg=Config()))
    assert result.is_error is False
    body = result.content
    assert body["reading_depth"] == "full_text"
    assert METHODS in body["text"]
    sources = collect_sources(body, tool="read_paper")
    cited = "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9/"
    cards = audit_evidence(
        [{"url": cited, "excerpt": METHODS, "relation": "supports", "support_level": "direct"}],
        sources,
    )
    assert cards[0]["quote_status"] == "quote_found"
    assert cards[0]["retrieval_level"] == "full_text"


@pytest.mark.asyncio
async def test_record_without_open_text_stays_abstract_only(monkeypatch):
    async def fake_fetch(self, url, params=None):
        payload = {"resultList": {"result": [{
            "title": "Closed paper",
            "abstractText": "Only an abstract about rainfall variability.",
        }]}}
        return json.dumps(payload).encode(), url, "application/json"

    monkeypatch.setattr(ReadPaperTool, "_fetch", fake_fetch)
    result = await ReadPaperTool(Config()).call({"pmid": "12345"}, ToolCtx(cfg=Config()))
    assert result.content["reading_depth"] == "abstract_only"
    assert "rainfall variability" in result.content["text"]
    assert result.content["unavailable_reason"]
    sources = collect_sources(result.content, tool="read_paper")
    assert sources[0]["level"] == "abstract"


@pytest.mark.asyncio
async def test_read_paper_rejects_loopback():
    result = await ReadPaperTool(Config()).call(
        {"url": "http://127.0.0.1/secret.pdf"}, ToolCtx(cfg=Config()),
    )
    assert result.is_error
    assert "private" in (result.error_message or "").lower()


def test_generation_can_see_read_paper(tmp_cfg):
    names = {tool.name for tool in ToolRegistry(tmp_cfg).discover().tools_for("generation")}
    assert "read_paper" in names
