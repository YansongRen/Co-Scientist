"""Offline evidence provenance, separate from a model's scientific judgment.

Checks only content actually returned by tools. Finding a quotation does not
establish that it supports a claim. No fetching or model calls happen here.
"""
from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Any

from ..tools.urls import URL_KEYS

NOVELTY_POLICY = (
    "Empty search results do not establish novelty or the absence of published evidence. "
    "They may reflect query wording, database coverage, access failures, or limited search scope. "
    "After a bounded search, record a provisional hypothesis and explicitly state that novelty "
    "is unverified. Describe the databases and queries used, distinguish failed requests from "
    "successful searches with zero results, and state remaining evidence gaps. "
    "Do not fabricate citations or treat inaccessible sources as verified."
)


def collect_sources(body: Any, *, tool: str, failed: bool = False) -> list[dict]:
    if failed:
        return []
    result = []

    def walk(node):
        if isinstance(node, dict):
            urls = [node[k] for k in URL_KEYS if isinstance(node.get(k), str)
                    and node[k].startswith(("https://", "http://"))]
            if tool == "read_paper":
                urls.extend(u for u in node.get("aliases") or []
                            if isinstance(u, str) and u.startswith(("https://", "http://")))
                urls = list(dict.fromkeys(urls))
            text = next((node[k] for k in ("text", "abstract", "snippet", "content")
                         if isinstance(node.get(k), str) and node[k]), "")
            if tool == "read_paper" and node.get("reading_depth") == "full_text":
                level = "full_text"
            elif tool == "read_paper":
                level = "abstract"
            elif tool == "web_fetch":
                level = "fetched_text"
            else:
                level = "abstract" if node.get("abstract") else "search_snippet"
            for url in urls:
                result.append({"url": url, "title": str(node.get("title") or "")[:500],
                               "text": text[:120_000], "level": level})
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(body)
    return result[:100]


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def audit_evidence(items: list, sources: list[dict], *, molecule="unknown", default_claim="") -> list[dict]:
    cards = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        url = item["url"]
        if not url.startswith(("http://", "https://")):
            continue
        observed = [source for source in sources if source["url"] == url]
        excerpt = str(item.get("excerpt") or "")[:6000]
        quote = _normalize(excerpt)
        match = next((s for s in observed if len(quote) >= 25 and quote in _normalize(s["text"])), None)
        source = match or next((s for s in observed if s["text"]), {})
        title = str(item.get("title") or source.get("title") or "")
        source_scope = "unknown"
        warning = ""
        status = ("quote_found" if match else "no_excerpt" if not quote else
                  "quote_too_short" if len(quote) < 25 else
                  "quote_not_found" if source.get("text") else "not_checked")
        cards.append({"url": url, "title": title or url,
            "claim": str(item.get("claim") or default_claim)[:3000], "excerpt": excerpt,
            "relation": item.get("relation") if item.get("relation") in
                ("supports", "contradicts", "context", "uncertain") else "uncertain",
            "support_level": item.get("support_level") if item.get("support_level") in
                ("direct", "indirect", "background") else "unknown",
            "scope": str(item.get("scope") or ""), "source_molecule": source_scope,
            "scope_warning": warning, "quote_status": status,
            "retrieval_level": source.get("level", "unknown"),
            "semantic_status": "needs_human_review"})
    return cards


def annotate_record(record: dict, result, *, molecule="unknown", review=False) -> None:
    record["evidence_audit"] = audit_evidence(
        record.get("evidence" if review else "citations", []),
        getattr(result, "source_observations", []), molecule=molecule,
        default_claim=record.get("statement", ""))
    record["audit_version"] = 1
    if not review:
        searches = []
        for call in result.tool_calls:
            if "search" not in call.get("name", ""):
                continue
            args = call.get("args") or {}
            searches.append({"tool": call["name"],
                "query": str(args.get("query", args.get("q", args.get("term", ""))))[:500],
                "status": "failed" if call.get("is_error") else
                          "empty" if call.get("result_count") == 0 else "completed",
                "result_count": call.get("result_count")})
        record["novelty_assessment"] = {"status": "unverified", "searches": searches,
            "recorded_at": datetime.now(UTC).isoformat(),
            "limitation": "检索无结果不代表文献不存在或新颖性已确认。"}


def audit_summary(record: dict) -> str:
    if record.get("audit_version") != 1:
        return ""
    lines = ["## Evidence audit (program metadata)",
             "Quotation matching is not scientific validation. Claim support and scope require human review."]
    if record.get("novelty_assessment"):
        lines.append("Novelty status: unverified; empty searches do not establish novelty.")
    cards = sorted(record.get("evidence_audit", []), key=lambda c: not bool(c.get("scope_warning")))
    for card in cards[:12]:
        lines.append(f"- {card['url']}: quote={card['quote_status']}; "
                     f"model_relation={card['relation']}; support_level={card['support_level']}.")
        if card.get("scope_warning"):
            lines.append(f"  Scope limitation: {card['scope_warning']}")
    return "\n".join(lines)
