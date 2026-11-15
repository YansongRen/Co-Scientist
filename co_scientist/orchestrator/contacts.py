"""Suggest research contacts only from names already present in stored excerpts.

The section is a reading aid for the scientist. It is not a verified directory,
and the system does not send mail or infer an affiliation.
"""
from __future__ import annotations

import re

_ET_AL = re.compile(r"\b([A-Z][A-Za-z'\-]{2,}) et al\.")
_CN_ET_AL = re.compile(r"([\u4e00-\u9fff]{2,3})等人")


def suggest_contacts(sources: list[dict]) -> str:
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        excerpt = str(source.get("excerpt") or "")
        url = str(source.get("url") or "")
        title = str(source.get("title") or "")
        if not excerpt or not url:
            continue
        names = [match.group(1) for match in _ET_AL.finditer(excerpt)]
        names.extend(match.group(1) for match in _CN_ET_AL.finditer(excerpt))
        for name in names:
            key = (name, url)
            if key in seen:
                continue
            seen.add(key)
            found.append((name, url, title))
    lines = [
        "# Research contacts",
        "",
        "下列姓名只来自已经记录的文献摘录。这不是已核实的合作名单，系统没有发信，也没有邮箱或所属机构。",
    ]
    if not found:
        lines.append("当前摘录里没有可核对的作者姓名（例如 “Surname et al.” 或 “某某等人”），因此不推荐联系人。")
        return "\n".join(lines)
    for name, url, title in found:
        label = f"{name} et al." if name.isascii() else f"{name}等人"
        lines.append(f"- {label} — 摘录出现在「{title[:160]}」({url})")
    return "\n".join(lines)
