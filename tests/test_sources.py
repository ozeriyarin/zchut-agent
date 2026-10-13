from __future__ import annotations

import hashlib
import io
import json
import shutil
import urllib.request
import urllib.response
from collections.abc import Mapping
from datetime import date
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from conftest import AVTALA, FIXTURES, PENSIA, ROOT, TAX
from zchut.cli import app
from zchut.config import Settings
from zchut.sources.chunk import PATH_SEP, chunk_page, split_text
from zchut.sources.fetch import FetchError, FetcherRouter, FixtureFetcher, HttpxFetcher
from zchut.sources.mediawiki import MediaWikiFetcher, urllib_opener
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
        PENSIA: ("CC-BY-SA-3.0", "2026-06-03", "פנסיה וחיסכון ארוך טווח לשכירים"),
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
        ("עודכן לאחרונה ב11:38, 13 באוקטובר 2026.", date(2026, 10, 13)),
        ("עודכן לאחרונה ב09:05, 3 במרץ 2025.", date(2025, 3, 3)),
        ("עודכן לאחרונה ב09:05, 31 בפברואר 2025.", None),
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


def test_parse_mediawiki_title_skips_article_type() -> None:
    html = (
        '<h1><span class="mw-page-title-main">קצבת זיקנה</span> <span>(הליך)</span></h1>'
        '<p>מחוץ לתוכן</p><div id="mw-content-text"><h2>א</h2><p>תוכן</p></div>'
    )
    parsed = parse_page(FetchedPage(url=AVTALA, html=html, fetched_at=date(2026, 10, 13)))
    assert parsed.title == "קצבת זיקנה"
    assert parsed.sections == (Section(("א",), "תוכן"),)


def test_httpx_fetcher_honours_crawl_delay(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 5\n")
        return httpx.Response(200, text="<h1>כותרת</h1><p>טקסט</p>")

    sleeps: list[float] = []
    fetcher = HttpxFetcher(
        httpx.Client(transport=httpx.MockTransport(handler)),
        tmp_path / "raw",
        "zchut-agent/test",
        clock=lambda: 0.0,
        sleep=sleeps.append,
    )
    fetcher.fetch_page(AVTALA)
    assert sleeps == [5.0]


def test_httpx_fetcher_encodes_request_and_keeps_canonical_url(tmp_path: Path) -> None:
    url = "https://www.kolzchut.org.il/he/קצבת_זיקנה_(קצבת_אזרח_ותיק)"
    encoded = "https://www.kolzchut.org.il" + quote("/he/קצבת_זיקנה_(קצבת_אזרח_ותיק)", safe="/_()")
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.raw_path.decode("ascii"))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/he/old":
            return httpx.Response(301, headers={"Location": encoded})
        return httpx.Response(200, text="<h1>כותרת</h1><p>טקסט</p>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = HttpxFetcher(client, tmp_path / "raw", "zchut-agent/test", 0.0)
    sources = (url, encoded, "https://www.kolzchut.org.il/he/old")
    assert [fetcher.fetch_page(u).url for u in sources] == [url] * 3
    assert sent[1] == encoded.removeprefix("https://www.kolzchut.org.il")
    assert "%D7%A7" in sent[1] and sent[1].endswith("%D7%99%D7%A7)")


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


def _redirecting_fetcher(
    tmp_path: Path, redirects: dict[str, str], seen: list[str]
) -> HttpxFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        target = redirects.get(str(request.url))
        if target is not None:
            return httpx.Response(301, headers={"Location": target})
        return httpx.Response(200, text="<h1>כותרת</h1><p>טקסט</p>")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return HttpxFetcher(client, tmp_path / "raw", "zchut-agent/test", 0.0)


def test_httpx_fetcher_follows_same_host_redirect(tmp_path: Path) -> None:
    seen: list[str] = []
    fetcher = _redirecting_fetcher(tmp_path, {"https://www.gov.il/he/old": "/he/new"}, seen)
    fetched = fetcher.fetch_page("https://www.gov.il/he/old")
    assert fetched.url == "https://www.gov.il/he/new"
    name = hashlib.sha256(fetched.url.encode("utf-8")).hexdigest()[:16]
    assert [p.name for p in (tmp_path / "raw").iterdir()] == [f"{name}.html"]


@pytest.mark.parametrize(
    "redirects",
    [
        {"https://www.gov.il/he/a": "https://evil.example/x"},
        {
            "https://www.gov.il/he/a": "https://evil.example/hop",
            "https://evil.example/hop": "https://www.gov.il/he/b",
        },
    ],
)
def test_httpx_fetcher_refuses_cross_host_redirect(
    tmp_path: Path, redirects: dict[str, str]
) -> None:
    seen: list[str] = []
    fetcher = _redirecting_fetcher(tmp_path, redirects, seen)
    with pytest.raises(FetchError, match="evil.example"):
        fetcher.fetch_page("https://www.gov.il/he/a")
    assert not (tmp_path / "raw").exists()
    assert all("evil.example" not in url for url in seen)


MW_FIXTURES = ROOT / "tests" / "fixtures" / "mediawiki"
UA = "zchut-agent/test"


def _wiki_fetcher(
    tmp_path: Path,
    robots: bytes,
    seen: list[str],
    sleeps: list[float],
    posted: list[str] | None = None,
) -> MediaWikiFetcher:
    posted = [] if posted is None else posted

    def opener(url: str, headers: Mapping[str, str], data: bytes | None = None) -> bytes:
        assert headers["User-Agent"] == UA
        seen.append(url)
        if data is not None:
            assert headers["Content-Type"] == "application/x-www-form-urlencoded"
            posted.append(data.decode("utf-8"))
            return (MW_FIXTURES / "revisions.json").read_bytes()
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return robots
        action = parse_qs(parts.query)["action"][0]
        return (
            MW_FIXTURES / {"parse": "parse.json", "query": "revisions.json"}[action]
        ).read_bytes()

    return MediaWikiFetcher(
        tmp_path / "raw", UA, 1.0, opener, clock=lambda: 0.0, sleep=sleeps.append
    )


def test_mediawiki_fetcher_builds_a_page_from_the_api(tmp_path: Path) -> None:
    seen: list[str] = []
    fetcher = _wiki_fetcher(tmp_path, b"User-agent: *\nCrawl-delay: 5\n", seen, [])
    fetched = fetcher.fetch_page(quote(AVTALA, safe=":/"))
    assert fetched.url == AVTALA
    assert fetched.last_updated == date(2026, 10, 13)
    assert '<h2><span class="mw-headline" id="מי_זכאי">מי זכאי</span></h2>' in fetched.html
    queries = [parse_qs(urlsplit(u).query) for u in seen[1:]]
    assert [q["action"] for q in queries] == [["parse"], ["query"]]
    assert queries[0]["page"] == ["דמי_אבטלה"] and queries[1]["titles"] == ["דמי_אבטלה"]
    assert all(urlsplit(u).path == "/api.php" for u in seen[1:])
    assert len(list((tmp_path / "raw").iterdir())) == 2
    parsed = parse_page(fetched)
    assert (parsed.title, parsed.last_updated) == ("דמי אבטלה", date(2026, 10, 13))
    assert ("מי זכאי", "תקופת אכשרה") in [s.headings for s in parsed.sections]
    assert ("הרשמה בשירות התעסוקה",) in [s.headings for s in parsed.sections]


def test_mediawiki_fetcher_honours_crawl_delay_on_api_host(tmp_path: Path) -> None:
    seen: list[str] = []
    sleeps: list[float] = []
    fetcher = _wiki_fetcher(tmp_path, b"User-agent: *\nCrawl-delay: 5\n", seen, sleeps)
    fetcher.fetch_page(AVTALA)
    assert urlsplit(seen[0]).path == "/robots.txt"
    assert sleeps == [5.0, 5.0]


def test_mediawiki_fetch_article_with_known_revision_makes_one_api_request(
    tmp_path: Path,
) -> None:
    seen: list[str] = []
    sleeps: list[float] = []
    fetcher = _wiki_fetcher(tmp_path, b"User-agent: *\nCrawl-delay: 5\n", seen, sleeps)
    fetched = fetcher.fetch_article(AVTALA, date(2026, 9, 30))
    assert fetched.last_updated == date(2026, 9, 30)
    assert [parse_qs(urlsplit(u).query)["action"] for u in seen[1:]] == [["parse"]]
    assert sleeps == [5.0]


def test_mediawiki_api_post_sends_titles_in_a_form_body(tmp_path: Path) -> None:
    seen: list[str] = []
    sleeps: list[float] = []
    posted: list[str] = []
    fetcher = _wiki_fetcher(tmp_path, b"User-agent: *\nCrawl-delay: 5\n", seen, sleeps, posted)
    api = "https://www.kolzchut.org.il/api.php"
    text = fetcher.api_post(api, {"action": "query", "titles": "דמי אבטלה|פנסיה"})
    assert text == (MW_FIXTURES / "revisions.json").read_text(encoding="utf-8")
    assert seen == ["https://www.kolzchut.org.il/robots.txt", api]
    assert parse_qs(posted[0]) == {"action": ["query"], "titles": ["דמי אבטלה|פנסיה"]}
    assert sleeps == [5.0] and len(list((tmp_path / "raw").iterdir())) == 1


def test_mediawiki_api_post_honours_robots_disallow(tmp_path: Path) -> None:
    seen: list[str] = []
    fetcher = _wiki_fetcher(tmp_path, b"User-agent: *\nDisallow: /api.php\n", seen, [])
    with pytest.raises(FetchError, match="robots"):
        fetcher.api_post("https://www.kolzchut.org.il/api.php", {"action": "query"})
    assert [urlsplit(u).path for u in seen] == ["/robots.txt"]


def test_mediawiki_fetcher_honours_robots_disallow(tmp_path: Path) -> None:
    seen: list[str] = []
    fetcher = _wiki_fetcher(tmp_path, b"User-agent: *\nDisallow: /api.php\n", seen, [])
    with pytest.raises(FetchError, match="robots"):
        fetcher.fetch_page(AVTALA)
    assert [urlsplit(u).path for u in seen] == ["/robots.txt"]


def test_fetcher_router_picks_mediawiki_for_kolzchut_and_httpx_for_gov_il(
    tmp_path: Path,
) -> None:
    wiki = _wiki_fetcher(tmp_path, b"", [], [])
    web = HttpxFetcher(httpx.Client(), tmp_path / "raw", UA)
    router = FetcherRouter({"kolzchut.org.il": wiki}, web)
    assert router.for_url(AVTALA) is wiki
    assert router.for_url("https://kolzchut.org.il/he/x") is wiki
    assert router.for_url(TAX) is web
    assert router.for_url("https://notkolzchut.org.il/he/x") is web


def _serve_https(monkeypatch: pytest.MonkeyPatch, routes: dict[str, tuple[int, str]]) -> list[str]:
    seen: list[str] = []

    def https_open(
        self: urllib.request.HTTPSHandler, req: urllib.request.Request
    ) -> urllib.response.addinfourl:
        seen.append(req.full_url)
        code, target = routes[req.full_url]
        headers = Message()
        if code != 200:
            headers["Location"] = target
        body = target.encode() if code == 200 else b""
        response = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, code)
        response.msg = "stub"
        return response

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    return seen


def test_urllib_opener_follows_same_host_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve_https(
        monkeypatch,
        {AVTALA: (302, "/he/b"), "https://www.kolzchut.org.il/he/b": (200, "ok")},
    )
    assert urllib_opener(1.0)(AVTALA, {"User-Agent": UA}) == b"ok"
    assert seen == [AVTALA, "https://www.kolzchut.org.il/he/b"]


@pytest.mark.parametrize("target", ["https://evil.example/he/b", "http://www.kolzchut.org.il/he/b"])
def test_urllib_opener_refuses_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    seen = _serve_https(monkeypatch, {AVTALA: (302, target), target: (200, "evil")})
    with pytest.raises(FetchError, match="redirect .* refused"):
        urllib_opener(1.0)(AVTALA, {"User-Agent": UA})
    assert seen == [AVTALA]


@pytest.mark.parametrize("code", [302, 307])
def test_urllib_opener_refuses_redirect_of_post(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    target = "https://www.kolzchut.org.il/he/b"
    seen = _serve_https(monkeypatch, {AVTALA: (code, target), target: (200, "ok")})
    with pytest.raises(FetchError, match="redirect of POST .* refused"):
        urllib_opener(1.0)(AVTALA, {"User-Agent": UA}, b"titles=a")
    assert seen == [AVTALA]


def test_urllib_opener_caps_same_host_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    hop = "https://www.kolzchut.org.il/he/hop{}"
    routes = {hop.format(i): (302, hop.format(i + 1)) for i in range(5)}
    seen = _serve_https(monkeypatch, routes)
    with pytest.raises(FetchError, match="failed"):
        urllib_opener(1.0)(hop.format(0), {"User-Agent": UA})
    assert len(seen) == 4


def test_urllib_opener_refuses_non_https_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve_https(monkeypatch, {})
    with pytest.raises(FetchError, match="non-https"):
        urllib_opener(1.0)("http://www.kolzchut.org.il/he/a", {"User-Agent": UA})
    assert seen == []
