"""Tests for the safety classifier's action mapping (no API calls)."""

from __future__ import annotations

import pytest

from co_scientist.config import Config, SafetyCfg
from co_scientist.llm.types import CaptureBundle, LLMResponse, SynthMessage, ToolUseBlock
from co_scientist.safety.classifier import ClassifierResult, SafetyClassifier


def test_benign_is_allowed() -> None:
    r = ClassifierResult(categories=["none"], confidence=1.0, rationale="ok")
    assert r.is_benign
    assert r.action(Config()) == "allow"


def test_block_categories_block() -> None:
    cfg = Config()
    r = ClassifierResult(categories=["cbrn"], confidence=0.99, rationale="x")
    assert not r.is_benign
    assert r.action(cfg) == "block"


def test_warn_high_confidence_quarantines() -> None:
    cfg = Config()
    r = ClassifierResult(categories=["dual_use_bio"], confidence=0.8, rationale="x")
    assert r.action(cfg) == "quarantine"


def test_warn_low_confidence_warns() -> None:
    cfg = Config()
    r = ClassifierResult(categories=["dual_use_bio"], confidence=0.4, rationale="x")
    assert r.action(cfg) == "warn"


def test_unflagged_other_category_allows() -> None:
    cfg = Config()
    r = ClassifierResult(categories=["unknown_label"], confidence=0.5, rationale="x")
    assert r.action(cfg) == "allow"


def _response(*, payload=None, capture=None) -> LLMResponse:
    content = []
    if payload is not None:
        content = [ToolUseBlock(name="record_safety_assessment", input=payload)]
    return LLMResponse(
        raw=SynthMessage(content=content, stop_reason="tool_use" if content else "end_turn"),
        transcript_id="offline", cost_usd=0, input_tokens=0, output_tokens=0,
        cache_read=0, cache_write=0, capture=capture,
    )


class _StubLLM:
    def __init__(self, resp: LLMResponse) -> None:
        self.resp = resp
        self.specs = []

    async def call(self, spec, ctx):
        self.specs.append((spec, ctx))
        return self.resp


@pytest.mark.asyncio
async def test_classify_reads_api_tool_payload() -> None:
    cfg = Config(safety=SafetyCfg(enable_classifier=True))
    llm = _StubLLM(_response(payload={
        "categories": ["cbrn"], "confidence": 0.95, "rationale": "blocked",
    }))
    result = await SafetyClassifier(cfg).classify("dangerous goal", llm=llm, session_id="ses")
    assert result.action(cfg) == "block"
    assert llm.specs[0][1].session_id == "ses"
    assert llm.specs[0][1].agent == "classifier"


@pytest.mark.asyncio
async def test_classify_reads_cli_capture_when_raw_has_no_tool() -> None:
    cfg = Config(safety=SafetyCfg(enable_classifier=True))
    capture = CaptureBundle(records=[("record_safety_assessment", {
        "categories": ["dual_use_bio"], "confidence": 0.8, "rationale": "flag",
    })])
    result = await SafetyClassifier(cfg).classify(
        "gain of function", llm=_StubLLM(_response(capture=capture)),
    )
    assert result.action(cfg) == "quarantine"


@pytest.mark.asyncio
async def test_classify_fail_open_without_assessment() -> None:
    cfg = Config(safety=SafetyCfg(enable_classifier=True))
    result = await SafetyClassifier(cfg).classify("benign", llm=_StubLLM(_response()))
    assert result.action(cfg) == "allow"
    assert "no safety assessment" in result.rationale
