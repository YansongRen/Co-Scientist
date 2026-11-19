"""One literature pass on the session model, then shared with every author.

Retrieval stays on the session provider. Reading notes follow the quick mode of
norman-paper-reading v1.4.0 (N同学): a field survey records the question,
design, reported results, evidence gaps, and one next check. The skill reserves
full figure-by-figure close reading for a specified paper, so this pass does
not claim that mode. Text comes from the existing read_paper tool.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..agents.base import AgentDeps
from ..agents.base import BaseAgent
from ..llm.routing import route
from ..llm.tool_loop import ToolLoopExhausted, run_tool_loop
from ..llm.types import AgentCallSpec, CachedBlock, CallContext
from ..logging import get_logger
from ..safety.evidence import NOVELTY_POLICY
from ..storage.artifacts import read_json, write_json, write_text
from ..storage.repos import events

log = get_logger("literature")

SKILL_NAME = "norman-paper-reading"
SKILL_VERSION = "1.4.0"
SKILL_MODE = "quick"
WATERMARK = (
    "分析采用 norman-paper-reading 技能｜作者：N同学｜小红书：N同学｜B站/抖音：耐心的N同学\n"
    "Analysis performed using the norman-paper-reading skill | Author: N同学 | "
    "Xiaohongshu: N同学 | Bilibili/Douyin: 耐心的N同学"
)
_DEPTHS = {"full_text", "abstract_only", "unavailable"}
_FIELDS = ("question", "design", "results", "gaps", "next_step", "reading_priority")

RECORD_SHARED_LITERATURE: dict[str, Any] = {
    "name": "record_shared_literature",
    "description": (
        "Save the shared reading notes. Call this once after searching and reading. "
        "Do not register a hypothesis."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "overview": {"type": "string"},
            "papers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "reading_depth": {
                            "type": "string",
                            "enum": ["full_text", "abstract_only", "unavailable"],
                        },
                        "question": {"type": "string"},
                        "design": {"type": "string"},
                        "results": {"type": "string"},
                        "gaps": {"type": "string"},
                        "next_step": {"type": "string"},
                        "reading_priority": {"type": "string"},
                    },
                    "required": ["title", "url", "reading_depth", "question", "results"],
                },
            },
        },
        "required": ["overview", "papers"],
    },
}


def _clip(value: Any, limit: int = 900) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_papers(raw: Any, observations: list[dict]) -> list[dict]:
    """Keep papers whose URL was actually returned, and do not upgrade depth."""
    full_text = {item.get("url") for item in observations if item.get("level") == "full_text"}
    seen = {item.get("url") for item in observations if item.get("url")}
    papers = []
    if not isinstance(raw, list):
        return papers
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or url not in seen:
            continue
        depth = item.get("reading_depth")
        if depth not in _DEPTHS or (depth == "full_text" and url not in full_text):
            depth = "abstract_only" if url in seen else "unavailable"
        papers.append({
            "title": _clip(item.get("title"), 300),
            "url": url,
            "reading_depth": depth,
            **{key: _clip(item.get(key)) for key in _FIELDS},
        })
    return papers


def render_brief_markdown(record: dict) -> str:
    lines = [
        "# 共享文献阅读",
        "",
        f"阅读方式：{SKILL_NAME} {SKILL_VERSION} · {SKILL_MODE}。",
        "这是给后续假设使用的共享笔记。摘要或检索片段不能写成已读全文。",
        "",
        _clip(record.get("overview"), 1600) or "没有形成概述。",
        "",
    ]
    for index, paper in enumerate(record.get("papers") or [], start=1):
        depth = {
            "full_text": "已读开放全文",
            "abstract_only": "只读到摘要或片段",
            "unavailable": "未能读到正文",
        }.get(paper.get("reading_depth"), "只读到摘要或片段")
        lines += [
            f"## {index}. {paper.get('title') or '未命名文献'}",
            "",
            f"来源：{paper.get('url')}",
            f"阅读深度：{depth}",
            "",
            f"研究问题：{paper.get('question') or '笔记未写'}",
            "",
            f"核心设计：{paper.get('design') or '笔记未写'}",
            "",
            f"关键结果：{paper.get('results') or '笔记未写'}",
            "",
            f"证据缺口：{paper.get('gaps') or '笔记未写'}",
            "",
            f"下一步核对：{paper.get('next_step') or '笔记未写'}",
            "",
            f"阅读优先级：{paper.get('reading_priority') or '未标注'}",
            "",
        ]
    if record.get("error_type"):
        lines += [f"这次共享阅读没有完成（{record['error_type']}）。", ""]
    lines += [WATERMARK, ""]
    return "\n".join(lines)


def generation_context(
    record: dict | None, *, strategy: str | None = None, exclusive: bool = True,
) -> tuple[str, bool]:
    """Return the prompt block and whether this author may search.

    A missing, failed, or empty brief restores per-author search. A ready
    brief is exclusive for literature/debate/assumption when `exclusive` is
    true. `feedback_driven` may still search uncovered directions.
    """
    if not record:
        return "", True
    papers = record.get("papers") or []
    if record.get("status") != "ready" or not papers:
        err = record.get("error_type") or "unavailable"
        return (
            f"The shared literature pass did not complete ({err}). "
            "Search yourself using the available tools. Do not invent citations. "
            "If a search returns nothing, that does not confirm novelty.\n\n"
            + NOVELTY_POLICY
        ), True
    may_search = (not exclusive) or strategy == "feedback_driven"
    if may_search:
        lead = (
            "Shared reading notes follow. Prefer these papers. You may search for "
            "papers these notes do not cover. Do not repeat the shared queries. "
            "Cite URLs from the notes or from your own tool results. "
        )
    else:
        lead = (
            "The only literature pass is already complete. It was written by the session "
            f"model using {SKILL_NAME} {SKILL_MODE} mode. Do not search, fetch, or read again. "
            "Cite only URLs in the notes below. "
        )
    return (
        lead
        + "If a note's reading depth is not full text, "
        "do not describe methods or results the note does not contain.\n\n"
        + render_brief_markdown(record)
        + "\n" + NOVELTY_POLICY
    ), may_search


async def load_shared_literature(cfg, session_id: str) -> dict | None:
    rel = f"artifacts/{session_id}/literature/shared.json"
    try:
        record = await read_json(cfg, rel)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _remaining_seconds(session) -> float | None:
    deadline = getattr(session, "wall_deadline", None)
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return (deadline - datetime.now(UTC)).total_seconds()


async def gather_shared_literature(deps: AgentDeps, session) -> dict | None:
    """Search and read once on the session model. Never raises into startup."""
    if not deps.cfg.literature.enabled:
        return None
    existing = await load_shared_literature(deps.cfg, session.id)
    if existing:
        return existing
    remaining = _remaining_seconds(session)
    if remaining is not None and remaining < 90:
        record = _empty(deps, error_type="wall_clock")
        await _save(deps, session, record)
        return record
    try:
        record = await _gather(deps, session)
    except Exception as exc:
        log.error("literature_gather_failed", session_id=session.id, error_type=type(exc).__name__)
        record = _empty(deps, error_type=type(exc).__name__)
    await _save(deps, session, record)
    return record


def _empty(deps: AgentDeps, *, error_type: str) -> dict:
    chosen = route(deps.cfg, "parse_goal")
    return {
        "status": "failed",
        "skill": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "skill_mode": SKILL_MODE,
        "model": chosen.model,
        "endpoint": chosen.endpoint,
        "overview": "",
        "papers": [],
        "error_type": error_type,
    }


async def _gather(deps: AgentDeps, session) -> dict:
    chosen = route(deps.cfg, "parse_goal")
    goal = session.research_plan.objective if session.research_plan else session.research_goal
    limit = deps.cfg.literature.max_papers
    prompt = (
        "You are the only literature agent for this research. Search PubMed, Europe PMC, "
        "and arXiv, then call read_paper on each paper you will keep. Do not use web_search. "
        f"Keep at most {limit} papers and only a handful of queries. "
        f"Write each note in {SKILL_NAME} {SKILL_VERSION} {SKILL_MODE} mode: research question, "
        "core design, key reported results, evidence gaps, one useful next check, and a reading "
        "priority. This is a survey, not a figure-by-figure close reading. "
        "If reading_depth is not full_text, do not describe methods or results you did not receive. "
        "Do not invent metadata, page numbers, or unseen results. "
        "Then call record_shared_literature once. Do not register a hypothesis.\n\n"
        f"Goal:\n{goal}"
    )
    search_tools = [
        tool for tool in deps.tools.anthropic_tools_for("generation")
        if tool["name"] in {
            "pubmed_search", "arxiv_search", "europe_pmc_search",
            "read_paper",
        }
    ]
    spec = AgentCallSpec(
        route=chosen,
        system_blocks=[CachedBlock(
            f"Shared literature reading with {SKILL_NAME} {SKILL_MODE} mode. "
            "Search results are not full-text reading.",
            cache=True,
        )],
        user_blocks=[CachedBlock(prompt, cache=False)],
        tools=[*search_tools, RECORD_SHARED_LITERATURE],
        tool_choice={"type": "auto"},
        max_output_tokens=2048,
    )
    ctx = CallContext(
        session_id=session.id, task_id=None, agent="supervisor",
        action="GatherLiterature", mode=SKILL_MODE,
    )
    try:
        loop = await run_tool_loop(
            deps.llm, spec=spec, ctx=ctx, registry=deps.tools,
            max_iters=deps.cfg.literature.max_iters,
            parallel_cap=1,
            tool_timeout_s=deps.cfg.tool_loop.tool_timeout_seconds,
            force_terminal_tool="record_shared_literature",
            terminal_tool_names=("record_shared_literature",),
        )
    except ToolLoopExhausted as exc:
        raise RuntimeError("literature tool loop exhausted") from exc
    raw = BaseAgent._final_tool_use(loop.response, "record_shared_literature") or {}
    papers = normalize_papers(raw.get("papers"), loop.source_observations)[:limit]
    return {
        "status": "ready",
        "skill": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "skill_mode": SKILL_MODE,
        "model": chosen.model,
        "endpoint": chosen.endpoint,
        "overview": _clip(raw.get("overview"), 1600),
        "papers": papers,
        "error_type": "",
    }


async def _save(deps: AgentDeps, session, record: dict) -> None:
    rel = await write_json(deps.cfg, session.id, "literature", "shared", record)
    await write_text(
        deps.cfg, session.id, "literature", "shared", ".md", render_brief_markdown(record),
    )
    execution = session.config_snapshot.setdefault("research_execution", {})
    execution["literature_brief"] = rel
    execution["literature_skill"] = SKILL_NAME
    execution["literature_mode"] = SKILL_MODE
    await deps.db.execute(
        "UPDATE sessions SET config_snapshot=?, updated_at=? WHERE id=?",
        (json.dumps(session.config_snapshot), datetime.now(UTC).isoformat(), session.id),
    )
    await deps.db.commit()
    await events.emit(
        deps.db, session_id=session.id, task_id=None, agent="supervisor",
        event="literature_gathered",
        payload={
            "status": record.get("status"),
            "paper_count": len(record.get("papers") or []),
            "skill": SKILL_NAME,
            "mode": SKILL_MODE,
            "model": record.get("model"),
            "error_type": record.get("error_type") or "",
        },
    )
