from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from conftest import AVTALA, FIXTURES, PENSIA, TAX
from zchut.cli import app
from zchut.config import Settings
from zchut.sources.chunk import PATH_SEP, chunk_page, split_text
from zchut.sources.fetch import FetchError, FixtureFetcher, HttpxFetcher
from zchut.sources.parse import ParseError, parse_last_updated, parse_page
from zchut.types import Chunk, FetchedPage, Page, Section

runner = CliRunner()


def fixture_chunks(settings: Settings) -> dict[str, list[Chunk]]:
    fetcher = FixtureFetcher(FIXTURES)
    return {
        url: chunk_page(parse_page(fetcher.fetch_page(url)), **settings.chunk.model_dump())
        for url in fetcher.urls()
    }


def test_criterion_1_ingest_fixtures_dry_run(cli_root: Path) -> None:
    """criterion 1: "`zchut ingest --from-fixtures tests/fixtures/pages --dry-run` indexes the three saved pages (two kolzchut, one gov.il) with the fake embedder and the in-memory store, prints the page and chunk counts, and a page that fails to parse is listed as an error while the other pages are still indexed." """
    result = runner.invoke(app, ["ingest", "--from-fixtures", "tests/fixtures/pages", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "עמודים: 3" in result.output
    assert "שגיאות: 0" in result.output
    record = json.loads(next((cli_root / "runs").glob("*-ingest.json")).read_text("utf-8"))
    assert record["pages_indexed"] == 3 and record["chunks_indexed"] > 3
    assert f"מקטעים: {record['chunks_indexed']}" in result.output
    assert {c["model"] for c in record["calls"]} == {"fake-embedder"}

    broken = cli_root / "broken"
    shutil.copytree(FIXTURES, broken)
    (broken / "bad.html").write_text("<html><body><nav>רק תפריט</nav></body></html>", "utf-8")
    bad_url = "https://www.kolzchut.org.il/he/שבור"
    meta = {"url": bad_url, "file": "bad.html", "fetched_at": "2026-10-13"}
    (broken / "bad.meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    result = runner.invoke(app, ["ingest", "--from-fixtures", "broken", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "עמודים: 3" in result.output
    assert "שגיאות: 1" in result.output
    assert bad_url in result.output


def test_criterion_2_chunking_by_sections(settings: Settings) -> None:
    """criterion 2: "Chunking: each fixture page is split by its section headings, every chunk carries the title path (`<page title> > <h2> > <h3>`), short sibling sections are merged, and no chunk exceeds `chunk.max_chars`." """
    chunks = fixture_chunks(settings)
    sections = {c.section for c in chunks[AVTALA]}
    assert "דמי אבטלה > הרשמה בשירות התעסוקה > מה צריך להביא לרישום בלשכה" in sections
    assert "דמי אבטלה > מי זכאי" in sections
    for page_chunks in chunks.values():
        assert len(page_chunks) > 1
        for c in page_chunks:
            assert c.section.split(PATH_SEP)[0] == c.title
            assert len(c.section.split(PATH_SEP)) <= 3
            assert len(c.text) <= settings.chunk.max_chars
    merged = [c for c in chunks[AVTALA] if "התפטרות" in c.text and "גיל\n" in c.text]
    assert [c.section for c in merged] == ["דמי אבטלה > מי זכאי > תקופת אכשרה / התפטרות / גיל"]
    small = chunk_page(parse_page(FixtureFetcher(FIXTURES).fetch_page(PENSIA)), 120, 0)
    assert all(len(c.text) <= 120 for c in small) and len(small) > len(chunks[PENSIA])


def test_criterion_3_chunk_metadata(settings: Settings) -> None:
    """criterion 3: "Every chunk has `url`, `title`, `section`, `fetched_at` (ISO date) and `license` (`CC-BY-SA-3.0` for kolzchut, `gov.il` for gov.il), plus `last_updated` when the page states it and `None` when it does not." """
    chunks = fixture_chunks(settings)
    expected = {
        AVTALA: ("CC-BY-SA-3.0", "2026-08-12", "דמי אבטלה"),
        PENSIA: ("CC-BY-SA-3.0", "2026-06-03", "פנסיית חובה"),
        TAX: ("gov.il", None, "הגשת בקשה להחזר מס הכנסה לשכירים"),
    }
    for url, (license_id, updated, title) in expected.items():
        for c in chunks[url]:
            assert (c.url, c.title, c.license, c.last_updated) == (url, title, license_id, updated)
            assert date.fromisoformat(c.fetched_at) == date(2026, 10, 13)
            assert c.section and c.text


def page(*sections: tuple[tuple[str, ...], str]) -> Page:
    return Page(
        url=AVTALA,
        title="כותרת",
        license="CC-BY-SA-3.0",
        fetched_at=date(2026, 10, 13),
        last_updated=None,
        sections=tuple(Section(h, t) for h, t in sections),
    )


@pytest.mark.parametrize(
    ("sections", "max_chars", "min_chars", "expected"),
    [
        ([(("א",), "קצר"), (("ב",), "גם קצר")], 100, 50, ["כותרת > א / ב"]),
        ([(("א",), "קצר"), (("ב",), "גם קצר")], 100, 0, ["כותרת > א", "כותרת > ב"]),
        ([(("א",), "x" * 60), (("ב",), "y" * 60)], 100, 50, ["כותרת > א", "כותרת > ב"]),
        ([(("א", "1"), "קצר"), (("ב", "2"), "קצר")], 100, 50, ["כותרת > א > 1", "כותרת > ב > 2"]),
        ([((), "פתיח"), (("א",), "קצר")], 100, 50, ["כותרת", "כותרת > א"]),
    ],
    ids=["merge-short", "no-merge-when-min-zero", "too-long-to-merge", "cousins", "intro"],
)
def test_chunk_merging(
    sections: list[tuple[tuple[str, ...], str]], max_chars: int, min_chars: int, expected: list[str]
) -> None:
    assert [c.section for c in chunk_page(page(*sections), max_chars, min_chars)] == expected


@pytest.mark.parametrize(
    ("text", "max_chars", "expected"),
    [
        ("שורה אחת", 100, ["שורה אחת"]),
        ("אחת\nשתיים", 100, ["אחת\nשתיים"]),
        ("אחת\nשתיים", 5, ["אחת", "שתיים"]),
        ("אבג דהו זחט", 7, ["אבג דהו", "זחט"]),
        ("א" * 12, 5, ["אאאאא", "אאאאא", "אא"]),
        ("\n\n", 5, []),
    ],
    ids=["fits", "joins-lines", "splits-lines", "splits-on-space", "hard-split", "blank"],
)
def test_split_text(text: str, max_chars: int, expected: list[str]) -> None:
    assert split_text(text, max_chars) == expected


def test_chunk_ids_are_unique_and_stable(settings: Settings) -> None:
    first, second = fixture_chunks(settings), fixture_chunks(settings)
    ids = [c.id for cs in first.values() for c in cs]
    assert len(ids) == len(set(ids))
    assert ids == [c.id for cs in second.values() for c in cs]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("עודכן לאחרונה: 12.8.2026", date(2026, 8, 12)),
        ("עדכון אחרון 1/2/2025", date(2025, 2, 1)),
        ("עודכן לאחרונה: 31.2.2026", None),
        ("אין תאריך", None),
    ],
)
def test_parse_last_updated(text: str, expected: date | None) -> None:
    assert parse_last_updated(text) == expected


@pytest.mark.parametrize(
    ("url", "html"),
    [
        ("https://example.com/x", "<h1>כותרת</h1><p>טקסט</p>"),
        ("https://www.gov.il/he/x", "<html><body><p></p></body></html>"),
        ("https://www.gov.il/he/x", "<html><body><h1>כותרת</h1></body></html>"),
    ],
    ids=["foreign-host", "no-title", "no-sections"],
)
def test_parse_rejects(url: str, html: str) -> None:
    with pytest.raises(ParseError):
        parse_page(FetchedPage(url=url, html=html, fetched_at=date(2026, 10, 13)))


def test_parse_skips_noise_and_nested_blocks() -> None:
    html = (
        "<html><body><nav><p>תפריט</p></nav><main><h1>כותרת</h1><h2>א</h2>"
        "<ul><li><p>פריט</p></li></ul><script>x()</script></main></body></html>"
    )
    parsed = parse_page(FetchedPage(url=TAX, html=html, fetched_at=date(2026, 10, 13)))
    assert parsed.title == "כותרת"
    assert parsed.sections == (Section(("א",), "פריט"),)


def test_httpx_fetcher_robots_rate_limit_and_cache(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["User-Agent"] == "zchut-agent/test"
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="<h1>כותרת</h1><p>טקסט</p>")

    clock = iter([0.0, 0.2, 0.2])
    sleeps: list[float] = []
    fetcher = HttpxFetcher(
        httpx.Client(transport=httpx.MockTransport(handler)),
        tmp_path / "raw",
        "zchut-agent/test",
        1.0,
        clock=lambda: next(clock),
        sleep=sleeps.append,
    )
    fetched = fetcher.fetch_page("https://www.gov.il/he/a")
    assert fetched.html.startswith("<h1>")
    assert len(list((tmp_path / "raw").iterdir())) == 1
    assert sleeps == [pytest.approx(0.8)]
    with pytest.raises(FetchError, match="robots"):
        fetcher.fetch_page("https://www.gov.il/private/b")
    assert seen == ["/robots.txt", "/he/a"]
