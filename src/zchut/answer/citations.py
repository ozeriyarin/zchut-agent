from __future__ import annotations

import re
from collections.abc import Sequence

from zchut.answer.prompt import format_date
from zchut.types import Answer, Citation, Hit

ATTRIBUTION_LINE = (
    "התוכן מאתר כל-זכות (kolzchut.org.il) מוגש לפי רישיון Creative Commons ייחוס-שיתוף זהה 3.0 "
    "(CC BY-SA 3.0)."
)
_GROUP = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
KOLZCHUT_LICENSE = "CC-BY-SA-3.0"


def resolve(text: str, hits: Sequence[Hit]) -> Answer:
    valid = range(1, len(hits) + 1)
    order: list[int] = []
    dropped = 0

    def keep(match: re.Match[str]) -> str:
        nonlocal dropped
        numbers = [int(n) for n in re.split(r"\s*,\s*", match.group(1))]
        kept = [n for n in numbers if n in valid]
        dropped += len(numbers) - len(kept)
        order.extend(n for n in kept if n not in order)
        return f"[{', '.join(str(n) for n in kept)}]" if kept else ""

    cleaned = _GROUP.sub(keep, text)
    cleaned = re.sub(r"[ \t]+([.,:;!?])", r"\1", re.sub(r"[ \t]{2,}", " ", cleaned)).strip()
    citations = tuple(Citation(number=n, chunk=hits[n - 1].chunk) for n in order)
    return Answer(text=cleaned, citations=citations, dropped_citations=dropped)


def sources_block(citations: Sequence[Citation]) -> str:
    if not citations:
        return ""
    by_url: dict[str, list[Citation]] = {}
    for c in sorted(citations, key=lambda c: c.number):
        by_url.setdefault(c.chunk.url, []).append(c)
    lines = ["מקורות:"]
    for url, group in by_url.items():
        numbers = ", ".join(str(c.number) for c in group)
        updated = format_date(group[0].chunk.last_updated)
        suffix = f" (עודכן: {updated})" if updated else ""
        lines.append(f"[{numbers}] {group[0].chunk.title} — {url}{suffix}")
    if any(c.chunk.license == KOLZCHUT_LICENSE for c in citations):
        lines.append(ATTRIBUTION_LINE)
    return "\n".join(lines)


def render(answer: Answer) -> str:
    block = sources_block(answer.citations)
    return f"{answer.text}\n\n{block}" if block else answer.text
