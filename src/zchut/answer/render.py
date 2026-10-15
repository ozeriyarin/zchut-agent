from __future__ import annotations

import re
import unicodedata
from html import escape
from itertools import groupby

from zchut.answer.citations import (
    ATTRIBUTION_LINE,
    needs_attribution,
    source_groups,
    updated_suffix,
)
from zchut.types import Answer

_BOLD = re.compile(r"\*\*")
_LEADING = re.compile(r"^[ \t]*(?:#{1,6}|[-*•])[ \t]+", re.M)
_BLANKS = re.compile(r"\n(?:[ \t]*\n)+")


def foreign(ch: str) -> bool:
    script = unicodedata.name(ch, "").startswith(("HEBREW", "LATIN"))
    return unicodedata.category(ch)[0] in "LM" and not script


def clean_answer(text: str) -> tuple[str, int, int]:
    runs = [(bad, "".join(g)) for bad, g in groupby(text, foreign)]
    text = "".join(part for bad, part in runs if not bad)
    text, leading = _LEADING.subn("", text)
    text, bold = _BOLD.subn("", text)
    return _BLANKS.sub("\n\n", text).strip(), leading + bold, sum(b for b, _ in runs)


def render_html(answer: Answer) -> str:
    text = escape(answer.text, quote=False)
    if not answer.citations:
        return text
    lines = ["<b>מקורות:</b>"]
    for numbers, chunk in source_groups(answer.citations):
        link = f'<a href="{escape(chunk.url)}">{escape(chunk.title, quote=False)}</a>'
        lines.append(f"[{numbers}] {link}{updated_suffix(chunk)}")
    if needs_attribution(answer.citations):
        lines.append(escape(ATTRIBUTION_LINE, quote=False))
    return text + "\n\n" + "\n".join(lines)
