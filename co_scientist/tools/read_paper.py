"""Read one paper's open full text, not only its abstract.

Search tools return titles and abstracts. This tool resolves an open article
body (PMC XML, arXiv HTML or PDF, or a direct URL) and returns the methods,
results, and discussion when those sections can be found. A paywalled paper
comes back as abstract-only. The model is not told that a landing page was the article.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from xml.etree import ElementTree as ET

import httpx

from ..ids import url_hash
from .base import ToolCtx, ToolResult
from .web_fetch import _extract_html, _extract_pdf, _is_private_ip

_HEADING = re.compile(
    r"(?im)^(?P<h>abstract|introduction|background|methods|materials and methods|"
    r"methodology|results|results and discussion|discussion|conclusion|conclusions|limitations)\b.*$"
)
_ARXIV = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/)?(?P<id>\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_PMC = re.compile(r"PMC\d+", re.I)
_PMID_URL = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.I)
_SECTION_BUCKETS = {
    "abstract": "abstract",
    "introduction": "introduction",
    "background": "introduction",
    "methods": "methods",
    "materials and methods": "methods",
    "methodology": "methods",
    "results": "results",
    "results and discussion": "results",
    "discussion": "discussion",
    "conclusion": "discussion",
    "conclusions": "discussion",
    "limitations": "discussion",
}


def reading_packet(text: str, *, section_chars: int = 3500) -> dict:
    """Prefer methods, results, and discussion over the opening pages."""
    body = text.replace("\r\n", "\n").strip()
    matches = list(_HEADING.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = _SECTION_BUCKETS[match.group("h").lower()]
        if key in sections:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chunk = body[match.end():end].strip()
        if chunk:
            sections[key] = chunk[:section_chars]
    found = [key for key in ("abstract", "methods", "results", "discussion") if key in sections]
    if found:
        shown = "\n\n".join(f"## {key}\n{sections[key]}" for key in found)
        depth = "full_text" if "methods" in sections or "results" in sections else "abstract_only"
    elif len(body) >= 8000:
        span = section_chars
        mid = max(0, len(body) // 2 - span // 2)
        late = max(0, len(body) - span)
        shown = (
            f"## opening\n{body[:span]}\n\n"
            f"## middle\n{body[mid:mid + span]}\n\n"
            f"## closing\n{body[late:late + span]}"
        )
        depth = "full_text"
        found = ["opening", "middle", "closing"]
    else:
        shown = body[:section_chars]
        depth = "abstract_only" if shown else "unavailable"
    return {
        "reading_depth": depth,
        "sections_found": found,
        "text": shown,
        "total_chars": len(body),
    }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def jats_text(xml_bytes: bytes) -> tuple[str, str]:
    """Return title and a heading-marked body from JATS or Europe PMC XML."""
    root = ET.fromstring(xml_bytes)
    for element in root.iter():
        element.tag = _local(element.tag)
    title_node = root.find(".//article-title")
    title = "".join(title_node.itertext()).strip() if title_node is not None else ""
    lines: list[str] = []

    def add_section(section: ET.Element) -> None:
        heading = section.find("title")
        if heading is not None and "".join(heading.itertext()).strip():
            lines.append("".join(heading.itertext()).strip())
        for paragraph in list(section):
            if _local(paragraph.tag) == "p":
                text = "".join(paragraph.itertext()).strip()
                if text:
                    lines.append(text)
            elif _local(paragraph.tag) == "sec":
                add_section(paragraph)

    abstract = root.find(".//abstract")
    if abstract is not None:
        lines.append("Abstract")
        text = "".join(abstract.itertext()).strip()
        if text:
            lines.append(text)
    body = root.find(".//body")
    if body is not None:
        sections = [child for child in list(body) if _local(child.tag) == "sec"]
        if sections:
            for section in sections:
                add_section(section)
        else:
            text = "".join(body.itertext()).strip()
            if text:
                lines.append(text)
    return title, "\n".join(lines)


def _arxiv_id(value: str) -> str | None:
    match = _ARXIV.search(value.strip())
    return match.group("id") if match else None


def _pmcid(value: str) -> str | None:
    match = _PMC.search(value.strip())
    return match.group(0).upper() if match else None


class ReadPaperTool:
    name = "read_paper"
    description = (
        "Read the open full text of one paper. Returns methods, results, and discussion when an "
        "open article body is available. Accepts url, doi, pmid, pmcid, or arxiv_id. "
        "reading_depth=full_text means those sections (or opening/middle/closing windows of a long "
        "unsectioned body) were read. reading_depth=abstract_only means the full text was not "
        "available and only the abstract or landing page was returned. Use offset to continue a "
        "truncated body. Do not describe methods or results that this tool did not return."
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "doi": {"type": "string"},
            "pmid": {"type": "string"},
            "pmcid": {"type": "string"},
            "arxiv_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "description": "Character offset into the raw body."},
            "section_chars": {"type": "integer", "minimum": 500, "maximum": 8000},
        },
    }

    def __init__(self, cfg) -> None:
        self._cfg = cfg

    async def call(self, args: dict, ctx: ToolCtx) -> ToolResult:
        started = time.monotonic()
        section_chars = int(args.get("section_chars") or 3500)
        offset = int(args.get("offset") or 0)
        try:
            identity, aliases = self._identity(args)
        except ValueError as exc:
            return ToolResult(is_error=True, error_message=str(exc))
        cached = await self._read_cache(ctx, identity)
        if cached is None:
            fetched = await self._resolve(args, aliases)
            if isinstance(fetched, ToolResult):
                return fetched
            cached = fetched
            await self._write_cache(ctx, identity, cached)
        text = cached.get("text") or ""
        if offset > 0:
            window = text[offset:offset + section_chars * 2]
            payload = {
                **{key: cached.get(key) for key in ("title", "url", "aliases")},
                "reading_depth": "full_text" if window else "unavailable",
                "text": window,
                "offset": offset,
                "next_offset": offset + len(window) if offset + len(window) < len(text) else None,
                "total_chars": len(text),
                "sections_found": ["continued"],
            }
        else:
            packet = reading_packet(text, section_chars=section_chars)
            payload = {
                "title": cached.get("title") or "",
                "url": cached.get("url") or "",
                "aliases": cached.get("aliases") or [],
                "source_kind": cached.get("source_kind") or "",
                **packet,
                "offset": 0,
                "next_offset": section_chars if packet["total_chars"] > len(packet["text"]) else None,
                "unavailable_reason": cached.get("unavailable_reason") or "",
            }
        return ToolResult(
            content=payload,
            duration_ms=int((time.monotonic() - started) * 1000),
            result_bytes=len(json.dumps(payload)),
        )

    def _identity(self, args: dict) -> tuple[str, list[str]]:
        aliases: list[str] = []
        url = str(args.get("url") or "").strip()
        doi = str(args.get("doi") or "").strip()
        pmid = str(args.get("pmid") or "").strip()
        pmcid = _pmcid(str(args.get("pmcid") or "")) or _pmcid(url)
        arxiv = _arxiv_id(str(args.get("arxiv_id") or "")) or _arxiv_id(url)
        if url:
            aliases.append(url)
        if not any((url, doi, pmid, pmcid, arxiv)):
            raise ValueError("read_paper requires a url, doi, pmid, pmcid, or arxiv_id")
        if url.startswith("http://") or url.startswith("https://"):
            pmid_match = _PMID_URL.search(url)
            if pmid_match and not pmid:
                pmid = pmid_match.group(1)
        if pmid.isdigit():
            aliases.append(f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
        if pmcid:
            aliases.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/")
        if arxiv:
            aliases.append(f"https://arxiv.org/abs/{arxiv}")
        if doi:
            aliases.append(f"https://doi.org/{doi.removeprefix('https://doi.org/')}")
        identity = pmcid or (f"arxiv:{arxiv}" if arxiv else "") or (f"pmid:{pmid}" if pmid else "") or doi or url
        return identity, list(dict.fromkeys(aliases))

    async def _resolve(self, args: dict, aliases: list[str]) -> dict | ToolResult:
        pmcid = _pmcid(str(args.get("pmcid") or "")) or _pmcid(str(args.get("url") or ""))
        arxiv = _arxiv_id(str(args.get("arxiv_id") or "")) or _arxiv_id(str(args.get("url") or ""))
        pmid = str(args.get("pmid") or "").strip()
        doi = str(args.get("doi") or "").strip().removeprefix("https://doi.org/")
        url = str(args.get("url") or "").strip()
        if not pmid and url:
            match = _PMID_URL.search(url)
            pmid = match.group(1) if match else ""
        if pmcid:
            return await self._from_pmc(pmcid, aliases)
        if arxiv:
            return await self._from_arxiv(arxiv, aliases)
        if pmid or doi:
            return await self._from_europepmc(pmid=pmid, doi=doi, aliases=aliases)
        if url.startswith(("http://", "https://")):
            return await self._from_url(url, aliases, source_kind="url")
        return ToolResult(is_error=True, error_message="could not resolve a paper to read")

    async def _from_pmc(self, pmcid: str, aliases: list[str]) -> dict | ToolResult:
        xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
        fetched = await self._fetch(xml_url)
        if isinstance(fetched, ToolResult):
            return fetched
        try:
            title, text = await asyncio.to_thread(jats_text, fetched[0])
        except ET.ParseError as exc:
            return ToolResult(is_error=True, error_message=f"full text XML could not be parsed: {exc}")
        if not text.strip():
            return self._unavailable(xml_url, aliases, "The open XML record had no article body.")
        return self._stored(url=xml_url, title=title, text=text, aliases=aliases, source_kind="pmc_xml")

    async def _from_arxiv(self, arxiv: str, aliases: list[str]) -> dict | ToolResult:
        html_url = f"https://arxiv.org/html/{arxiv}"
        html = await self._from_url(html_url, aliases, source_kind="arxiv_html")
        if isinstance(html, dict) and reading_packet(html["text"])["reading_depth"] == "full_text":
            return html
        pdf_url = f"https://arxiv.org/pdf/{arxiv}.pdf"
        pdf = await self._from_url(pdf_url, aliases, source_kind="arxiv_pdf")
        if isinstance(pdf, dict) and pdf.get("text"):
            return pdf
        return html if isinstance(html, dict) else pdf

    async def _from_europepmc(self, *, pmid: str, doi: str, aliases: list[str]) -> dict | ToolResult:
        query = f"EXT_ID:{pmid} AND SRC:MED" if pmid else f"DOI:{doi}"
        fetched = await self._fetch(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": query, "format": "json", "resultType": "core", "pageSize": "1"},
        )
        if isinstance(fetched, ToolResult):
            return fetched
        try:
            payload = json.loads(fetched[0].decode("utf-8", errors="replace"))
            result = (payload.get("resultList") or {}).get("result") or []
            hit = result[0] if result else {}
        except (json.JSONDecodeError, IndexError):
            hit = {}
        pmcid = _pmcid(str(hit.get("pmcid") or ""))
        abstract = str(hit.get("abstractText") or "")
        title = str(hit.get("title") or "")
        if pmcid:
            opened = await self._from_pmc(pmcid, aliases)
            if isinstance(opened, dict) and opened.get("text"):
                opened["title"] = opened.get("title") or title
                return opened
        if abstract:
            return self._stored(
                url=aliases[0] if aliases else "",
                title=title,
                text=f"Abstract\n{abstract}",
                aliases=aliases,
                source_kind="abstract",
                unavailable_reason="No open full text was listed for this record. Only the abstract was read.",
            )
        return self._unavailable(aliases[0] if aliases else "", aliases, "The record had no open full text and no abstract.")

    async def _from_url(self, url: str, aliases: list[str], *, source_kind: str) -> dict | ToolResult:
        fetched = await self._fetch(url)
        if isinstance(fetched, ToolResult):
            return fetched
        body, final_url, content_type = fetched
        try:
            stripped = body.lstrip()
            if "xml" in content_type or stripped.startswith((b"<?xml", b"<article", b"<pmc-articleset")):
                title, text = await asyncio.to_thread(jats_text, body)
            elif "pdf" in content_type or final_url.lower().endswith(".pdf"):
                title, text = "", await asyncio.to_thread(_extract_pdf, body)
            else:
                text, title = await asyncio.to_thread(
                    _extract_html, body.decode("utf-8", errors="replace"), final_url
                )
        except (ET.ParseError, Exception) as exc:
            return ToolResult(is_error=True, error_message=f"could not extract article text: {exc}")
        if not (text or "").strip():
            return self._unavailable(final_url, aliases, "The URL returned no readable article text.")
        return self._stored(url=final_url, title=title or "", text=text, aliases=[*aliases, final_url], source_kind=source_kind)

    async def _fetch(self, url: str, params: dict | None = None):
        host = httpx.URL(url).host or ""
        if not host or await asyncio.to_thread(_is_private_ip, host):
            return ToolResult(is_error=True, error_message="URL resolves to a private/loopback address")
        max_bytes = self._cfg.web_fetch.max_bytes
        try:
            async with httpx.AsyncClient(
                timeout=self._cfg.web_fetch.timeout_seconds,
                follow_redirects=True,
                max_redirects=5,
                headers={"User-Agent": self._cfg.web_fetch.user_agent},
            ) as client:
                response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            return ToolResult(is_error=True, error_message=f"fetch failed: {exc}")
        if response.status_code >= 400:
            return ToolResult(is_error=True, error_message=f"HTTP {response.status_code}", content={"url": url, "status": response.status_code})
        if len(response.content) > max_bytes:
            return ToolResult(is_error=True, error_message=f"response too large (>{max_bytes} bytes)")
        final_host = response.url.host or ""
        if final_host and await asyncio.to_thread(_is_private_ip, final_host):
            return ToolResult(is_error=True, error_message="URL resolves to a private/loopback address")
        return response.content, str(response.url), (response.headers.get("content-type") or "").lower()

    @staticmethod
    def _stored(*, url: str, title: str, text: str, aliases: list[str], source_kind: str, unavailable_reason: str = "") -> dict:
        return {
            "url": url,
            "title": title,
            "text": text,
            "aliases": list(dict.fromkeys([*aliases, url])),
            "source_kind": source_kind,
            "unavailable_reason": unavailable_reason,
        }

    @staticmethod
    def _unavailable(url: str, aliases: list[str], reason: str) -> dict:
        return ReadPaperTool._stored(
            url=url, title="", text="", aliases=aliases, source_kind="unavailable", unavailable_reason=reason,
        )

    def _cache_path(self, ctx: ToolCtx, identity: str):
        if ctx.session_id is None:
            return None
        return self._cfg.session_artifact_dir(ctx.session_id) / "papers" / f"read-{url_hash(identity)}.json"

    async def _read_cache(self, ctx: ToolCtx, identity: str) -> dict | None:
        path = self._cache_path(ctx, identity)
        if path is None or not path.exists():
            return None
        return await asyncio.to_thread(lambda: json.loads(path.read_text()))

    async def _write_cache(self, ctx: ToolCtx, identity: str, payload: dict) -> None:
        path = self._cache_path(ctx, identity)
        if path is None:
            return

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False))
            temporary.replace(path)

        await asyncio.to_thread(write)
