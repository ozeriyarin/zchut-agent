from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from zchut.types import FetchedPage, Page, Section

LICENSES = {"kolzchut.org.il": "CC-BY-SA-3.0", "gov.il": "gov.il"}
_NOISE = ["script", "style", "nav", "header", "footer", "aside", "noscript"]
_BLOCKS = ["h2", "h3", "p", "li"]
_UPDATED = re.compile(r"(?:עודכן לאחרונה|עדכון אחרון)\D{0,20}(\d{1,2})[./](\d{1,2})[./](\d{4})")
_MONTHS = "ינואר פברואר מרץ אפריל מאי יוני יולי אוגוסט ספטמבר אוקטובר נובמבר דצמבר".split()
_UPDATED_MW = re.compile(
    r"עודכן לאחרונה\s*ב?\s*\d{1,2}:\d{2},\s*(\d{1,2})\s+ב?(" + "|".join(_MONTHS) + r")\s+(\d{4})"
)
_TITLE_SUFFIX = re.compile(r"\s*[-|–]\s*(?:כל-זכות|GOV\.IL)\s*$")


class ParseError(Exception):
    pass


def license_for(url: str) -> str:
    host = urlsplit(url).hostname or ""
    for domain, license_id in LICENSES.items():
        if host == domain or host.endswith("." + domain):
            return license_id
    raise ParseError(f"not an allowed public source: {url}")


def parse_last_updated(text: str) -> date | None:
    if (mw := _UPDATED_MW.search(text)) is not None:
        day, month, year = int(mw[1]), _MONTHS.index(mw[2]) + 1, int(mw[3])
    elif (match := _UPDATED.search(text)) is not None:
        day, month, year = (int(g) for g in match.groups())
    else:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _title(soup: BeautifulSoup) -> str:
    heading = soup.select_one("h1 .mw-page-title-main") or soup.find("h1")
    if isinstance(heading, Tag) and heading.get_text(strip=True):
        return heading.get_text(" ", strip=True)
    if soup.title is not None and soup.title.string:
        return _TITLE_SUFFIX.sub("", soup.title.string.strip())
    raise ParseError("page has no title")


def _sections(body: Tag) -> list[Section]:
    sections: list[Section] = []
    h2: str | None = None
    headings: tuple[str, ...] = ()
    lines: list[str] = []

    def flush() -> None:
        if lines:
            sections.append(Section(headings=headings, text="\n".join(lines)))

    for el in body.find_all(_BLOCKS):
        if el.find_parent(["p", "li"]) is not None:
            continue
        text = el.get_text(" ", strip=True)
        if el.name in ("h2", "h3"):
            flush()
            lines = []
            if el.name == "h2":
                h2 = text
                headings = (text,)
            else:
                headings = (h2, text) if h2 is not None else (text,)
        elif text:
            lines.append(text)
    flush()
    return sections


def parse_page(fetched: FetchedPage) -> Page:
    license_id = license_for(fetched.url)
    soup = BeautifulSoup(fetched.html, "html.parser")
    last_updated = parse_last_updated(soup.get_text(" ", strip=True))
    title = _title(soup)
    for tag in soup.find_all(_NOISE):
        tag.decompose()
    body = soup.find(id="mw-content-text") or soup.find("main") or soup.body
    if not isinstance(body, Tag):
        raise ParseError(f"no body in {fetched.url}")
    sections = _sections(body)
    if not sections:
        raise ParseError(f"no text sections in {fetched.url}")
    return Page(
        url=fetched.url,
        title=title,
        license=license_id,
        fetched_at=fetched.fetched_at,
        last_updated=last_updated,
        sections=tuple(sections),
    )
