"""Read-only presentation endpoints for the research workspace.

Starting research remains an explicit POST to the existing session endpoint.
No model is instantiated by this router; secrets/config snapshots stay server-side.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..inputs import list_files, public_file
from ..safety.evidence import audit_evidence, infer_molecule
from ..storage import db as db_mod
from ..storage.repos import events, hypotheses, reviews, sessions, transcripts
from .runtime import runtime_snapshot
from .sanitize import render_markdown

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
AGENTS = ("generation", "reflection", "ranking", "evolution", "proximity", "metareview")


def _read_artifact(cfg: Config, relative: str | None, *, json_record=False):
    if not relative:
        return {} if json_record else ""
    try:
        base = cfg.data_dir.resolve()
        path = (base / relative).resolve()
        path.relative_to(base)
        text = path.read_text(encoding="utf-8")
        if json_record:
            parsed = json.loads(text)
            record = parsed.get("record", {}) if isinstance(parsed, dict) else {}
            return record if isinstance(record, dict) else {}
        return text
    except (ValueError, OSError, UnicodeError):
        return {} if json_record else ""


def create_studio_router(cfg: Config) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    @router.get("/studio", response_class=HTMLResponse)
    async def workspace(request: Request):
        return TEMPLATES.TemplateResponse(request, "studio.html", {
            "defaults": {"budget": cfg.run.budget_usd, "wall_clock": cfg.run.wall_clock_seconds,
                         "budget_tokens": cfg.run.budget_tokens,
                         "model": "Mock" if getattr(request.app.state, "preview_only", False) else cfg.models.generation,
                         "pair_cap": cfg.ranking.lightweight_max_pair_matches,
                         "preview_only": bool(getattr(request.app.state, "preview_only", False))},
        })

    @router.get("/api/studio/sessions")
    async def session_list():
        conn = await db_mod.connect(cfg)
        try:
            async with conn.execute("""SELECT s.id,s.research_goal,s.status,s.created_at,s.updated_at,
                (SELECT COUNT(*) FROM hypotheses h WHERE h.session_id=s.id) AS hypothesis_count
                FROM sessions s ORDER BY s.created_at DESC LIMIT 50""") as cur:
                return {"sessions": [dict(row) for row in await cur.fetchall()]}
        finally:
            await conn.close()

    @router.get("/api/studio/sessions/{session_id}")
    async def session_workspace(session_id: str):
        conn = await db_mod.connect(cfg)
        try:
            session = await sessions.fetch(conn, session_id)
            if session is None:
                raise HTTPException(404, "Research session not found")
            hyps = await hypotheses.list_for_session(conn, session_id)
            all_reviews = await reviews.list_for_session(conn, session_id)
            usage = await transcripts.usage_summary(conn, session_id)
            sources: dict[str, dict] = {}
            evidence_cards = []
            novelty = []
            molecule = session.research_plan.target_molecule
            if molecule == "unknown":
                molecule = infer_molecule(session.research_goal)

            def collect_cards(record, h, *, review_id=None):
                cards = record.get("evidence_audit") if record.get("audit_version") == 1 else None
                if not isinstance(cards, list):
                    cards = audit_evidence(record.get("evidence" if review_id else "citations", []),
                        [], molecule=molecule, default_claim=h.summary)
                for index, card in enumerate(cards):
                    if not isinstance(card, dict):
                        continue
                    key = f"{h.id}:{review_id}:{index}:{card.get('url')}"
                    evidence_cards.append({**card, "id": hashlib.sha256(key.encode()).hexdigest()[:16],
                        "hypothesis_id": h.id, "hypothesis_title": h.title,
                        "origin": "reflection" if review_id else h.created_by,
                        "review_id": review_id, "legacy": record.get("audit_version") != 1})

            def add_sources(items):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url", "")
                    if isinstance(url, str) and url.startswith(("https://", "http://")):
                        sources.setdefault(url, {"url": url, "title": item.get("title") or item.get("claim") or url})

            result_hypotheses = []
            for h in hyps:
                record = _read_artifact(cfg, h.artifact_path, json_record=True)
                add_sources(record.get("citations", []))
                collect_cards(record, h)
                novelty.append({"hypothesis_id": h.id, "status": "unverified",
                    "searches": record.get("novelty_assessment", {}).get("searches", []),
                    "legacy": not bool(record.get("novelty_assessment"))})
                h_reviews = []
                for review in all_reviews:
                    if review.hypothesis_id != h.id:
                        continue
                    review_record = _read_artifact(cfg, review.artifact_path, json_record=True)
                    add_sources(review_record.get("evidence", []))
                    collect_cards(review_record, h, review_id=review.id)
                    h_reviews.append({"id": review.id, "verdict": review.verdict,
                        "scores": review.scores.model_dump(), "html": render_markdown(review.body)})
                result_hypotheses.append({"id": h.id, "title": h.title, "summary": h.summary,
                    "html": render_markdown(h.full_text), "elo": h.elo, "state": h.state,
                    "matches": h.matches_played, "strategy": h.strategy, "reviews": h_reviews,
                    "parent_ids": h.parent_ids, "novelty_status": "unverified"})
            report = _read_artifact(cfg, session.final_overview)
            # The report's S-tags refer to the ordered source block, so preserve
            # its order in the reference pane when a completed report exists.
            report_urls = re.findall(r"https?://[^\s<>\])]+", report)
            if report_urls:
                sources = {url: sources.get(url, {"url": url, "title": url}) for url in dict.fromkeys(report_urls)}
            for card in evidence_cards:
                known = sources.get(card["url"])
                if known and card["title"] == card["url"]:
                    card["title"] = known["title"]
                    scope = infer_molecule(card["title"] + " " + card["excerpt"])
                    card["source_molecule"] = scope
                    if molecule in ("DNA", "RNA") and scope in ("DNA", "RNA") and scope != molecule:
                        card["scope_warning"] = f"研究对象为 {molecule}; 此来源涉及 {scope}, 外推适用性尚未核验。"
                card["reference"] = f"S{list(sources).index(card['url'])+1}" if card["url"] in sources else "来源"
            run = session.config_snapshot.get("run", {})
            async with conn.execute("""SELECT agent,status,COUNT(*) n FROM tasks
                WHERE session_id=? GROUP BY agent,status""", (session_id,)) as cur:
                task_counts = [dict(r) for r in await cur.fetchall()]
            stages = []
            for agent in AGENTS:
                counts = {r["status"]: r["n"] for r in task_counts if r["agent"] == agent}
                status = ("running" if counts.get("in_progress") or counts.get("leased") else
                    "awaiting_approval" if counts.get("awaiting_approval") else
                    "pending" if counts.get("pending") else "done" if counts.get("done") else
                    "failed" if counts.get("dead") else "unused")
                stages.append({"agent": agent, "status": status, "count": counts.get("done", 0)})
            async with conn.execute("""SELECT id,created_at,hyp_a,hyp_b,mode,winner,rationale,
                elo_a_after,elo_b_after FROM tournament_matches WHERE session_id=?
                ORDER BY created_at DESC LIMIT 40""", (session_id,)) as cur:
                matches = [dict(r) for r in await cur.fetchall()]
            async with conn.execute("""SELECT id,agent,action,model,input_tokens,output_tokens,
                cost_usd,started_at,finished_at FROM transcripts WHERE session_id=?
                ORDER BY started_at DESC LIMIT 50""", (session_id,)) as cur:
                calls = [dict(r) for r in await cur.fetchall()]
            return {"session": {key: value for key, value in session.model_dump(mode="json").items()
                    if key != "config_snapshot"},
                "attachments": [public_file(cfg, row) for row in await list_files(conn, session.config_snapshot.get("web_launch", {}).get("upload_group_id"))],
                "hypotheses": result_hypotheses, "sources": list(sources.values()),
                "report": {"markdown": report, "html": render_markdown(report)},
                "stages": stages, "matches": matches, "calls": calls,
                "events": await events.recent(conn, session_id, limit=80), "usage": usage,
                "runtime": await runtime_snapshot(cfg, conn, session),
                "evidence": {"cards": evidence_cards, "novelty": novelty, "target_molecule": molecule,
                    "summary": {"total": len(evidence_cards),
                        "quotes_matched": sum(c["quote_status"] == "quote_found" for c in evidence_cards),
                        "scope_warnings": sum(bool(c["scope_warning"]) for c in evidence_cards),
                        "needs_review": sum(c["semantic_status"] == "needs_human_review" for c in evidence_cards)}},
                "policy": {"mode": run.get("research_mode", "standard"),
                    "max_ideas": run.get("max_ideas"), "concurrency": run.get("concurrency"),
                    "pair_cap": session.config_snapshot.get("ranking", {}).get("lightweight_max_pair_matches", 3)}}
        finally:
            await conn.close()

    @router.get("/api/studio/sessions/{session_id}/runtime")
    async def agent_runtime(session_id: str):
        conn = await db_mod.connect(cfg)
        try:
            session = await sessions.fetch(conn, session_id)
            if session is None:
                raise HTTPException(404, "Research session not found")
            return await runtime_snapshot(cfg, conn, session)
        finally:
            await conn.close()

    return router
