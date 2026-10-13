from __future__ import annotations

import re
from collections.abc import Sequence

from zchut.answer.prompt import format_date
from zchut.types import Answer, Chunk, Citation, Hit

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


def source_groups(citations: Sequence[Citation]) -> list[tuple[str, Chunk]]:
    by_url: dict[str, list[Citation]] = {}
    for c in sorted(citations, key=lambda c: c.number):
        by_url.setdefault(c.chunk.url, []).append(c)
    return [(", ".join(str(c.number) for c in g), g[0].chunk) for g in by_url.values()]


def updated_suffix(chunk: Chunk) -> str:
    updated = format_date(chunk.last_updated)
    return f" (עודכן: {updated})" if updated else ""


def needs_attribution(citations: Sequence[Citation]) -> bool:
    return any(c.chunk.license == KOLZCHUT_LICENSE for c in citations)


def sources_block(citations: Sequence[Citation]) -> str:
    if not citations:
        return ""
    lines = ["מקורות:"]
    for numbers, chunk in source_groups(citations):
        lines.append(f"[{numbers}] {chunk.title} — {chunk.url}{updated_suffix(chunk)}")
    if needs_attribution(citations):
        lines.append(ATTRIBUTION_LINE)
    return "\n".join(lines)


def render(answer: Answer) -> str:
    block = sources_block(answer.citations)
    return f"{answer.text}\n\n{block}" if block else answer.text
