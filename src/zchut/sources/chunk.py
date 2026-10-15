from __future__ import annotations

import hashlib
from itertools import groupby

from zchut.types import Chunk, Page, Section

PATH_SEP = " > "
MERGED_SEP = " / "


def _parent(section: Section) -> tuple[str, ...]:
    return section.headings[:-1]


def _render(group: list[Section]) -> str:
    if len(group) == 1:
        return group[0].text
    return "\n".join(f"{s.headings[-1]}\n{s.text}" for s in group)


def _merge_short(siblings: list[Section], max_chars: int, min_chars: int) -> list[list[Section]]:
    groups: list[list[Section]] = []
    for section in siblings:
        current = groups[-1] if groups else None
        if current is not None:
            short = len(_render(current)) < min_chars or len(section.text) < min_chars
            if short and len(_render([*current, section])) <= max_chars:
                current.append(section)
                continue
        groups.append([section])
    return groups


def split_text(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    for line in text.split("\n"):
        while len(line) > max_chars:
            cut = line.rfind(" ", 0, max_chars + 1)
            cut = cut if cut > 0 else max_chars
            pieces.append(line[:cut].strip())
            line = line[cut:].strip()
        if line:
            pieces.append(line)
    out: list[str] = []
    for piece in pieces:
        if out and len(out[-1]) + 1 + len(piece) <= max_chars:
            out[-1] = f"{out[-1]}\n{piece}"
        else:
            out.append(piece)
    return out


def make_chunk(page: Page, index: int, section: str, text: str) -> Chunk:
    prefix = hashlib.sha256(page.url.encode("utf-8")).hexdigest()[:12]
    return Chunk(
        id=f"{prefix}-{index:03d}",
        url=page.url,
        title=page.title,
        section=section,
        text=text,
        fetched_at=page.fetched_at.isoformat(),
        license=page.license,
        last_updated=page.last_updated.isoformat() if page.last_updated else None,
    )


def chunk_page(page: Page, max_chars: int, min_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    runs = groupby(page.sections, key=lambda s: (_parent(s), len(s.headings)))
    for _, run in runs:
        for group in _merge_short(list(run), max_chars, min_chars):
            path = group[0].headings
            if len(group) > 1:
                path = (*_parent(group[0]), MERGED_SEP.join(s.headings[-1] for s in group))
            section = PATH_SEP.join((page.title, *path))
            for text in split_text(_render(group), max_chars):
                chunks.append(make_chunk(page, len(chunks), section, text))
    return chunks
