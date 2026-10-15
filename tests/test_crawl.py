from __future__ import annotations

import json
import math
import shutil
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlsplit

import pytest
from typer.testing import CliRunner

from conftest import ROOT, make_guard
from zchut import cli
from zchut.cli import app
from zchut.config import ChunkSettings, ModelPrice, Prices, load_settings
from zchut.index.crawl import (
    CrawlDeps,
    CrawlPlan,
    PlannedArticle,
    estimate,
    format_plan,
    make_plan,
    run_crawl,
)
from zchut.index.embedder import FAKE_EMBED_MODEL, FakeEmbedder, InputType
from zchut.index.ingest import embed_text
from zchut.index.manifest import Manifest, load_manifest
from zchut.index.store import InMemoryVectorStore
from zchut.run.cost_guard import BudgetExceeded, CostGuard
from zchut.run.records import RunRecorder
from zchut.sources.catalog import Revision, latest_revisions, list_articles
from zchut.sources.chunk import chunk_page
from zchut.sources.fetch import FetchError
from zchut.sources.parse import parse_page
from zchut.tokens import estimate_tokens
from zchut.types import Chunk, CrawlRecord, EmbedResult, FetchedPage

runner = CliRunner()
MW = ROOT / "tests" / "fixtures" / "mediawiki"
MANIFEST = ROOT / "tests" / "fixtures" / "crawl" / "manifest.json"
API = "https://www.kolzchut.org.il/api.php"
SKIP = ["תבנית:", "קטגוריה:"]
WIKI = "https://www.kolzchut.org.il/he/"
REVS = {"דמי אבטלה": 100, "החזר מס הכנסה": 201, "פנסיה": 300, "קצבת זיקנה (קצבת אזרח ותיק)": 400}
STAMP = "2026-10-01T10:00:00Z"
CHUNK = ChunkSettings(max_chars=700, min_chars=100)
REMOVED = ROOT / "tests" / "fixtures" / "pages-short" / "kolzchut-removed.html"


class FakeApi:
    def __init__(self, revs: dict[str, int]) -> None:
        self.revs = revs
        self.queries: list[dict[str, list[str]]] = []
        self.posts: list[Mapping[str, str]] = []

    def api_get(self, url: str) -> str:
        assert url.startswith(API + "?")
        query = parse_qs(urlsplit(url).query)
        self.queries.append(query)
        assert query["list"] == ["allpages"]
        name = "allpages-2.json" if "apcontinue" in query else "allpages-1.json"
        return (MW / name).read_text(encoding="utf-8")

    def api_post(self, url: str, params: Mapping[str, str]) -> str:
        assert url == API
        assert params["action"] == "query" and params["prop"] == "revisions"
        self.posts.append(params)
        pages = [
            {"title": t, "revisions": [{"revid": self.revs[t], "timestamp": STAMP}]}
            if t in self.revs
            else {"title": t, "missing": True}
            for t in params["titles"].split("|")
        ]
        return json.dumps({"batchcomplete": True, "query": {"pages": pages}})


def page_html(title: str) -> str:
    body = f"{title}: כל עובד שכיר זכאי להפרשות מהמעסיק לאחר תקופת המתנה של שישה חודשים."
    return (
        f'<h1><span class="mw-page-title-main">{title}</span></h1>'
        f'<div id="mw-content-text"><h2>תנאים</h2><p>{body}</p></div>'
    )


class FakeArticles:
    def __init__(
        self,
        failing: Sequence[str] = (),
        clock: FakeClock | None = None,
        removed: Sequence[str] = (),
    ) -> None:
        self.fetched: list[str] = []
        self.failing = set(failing)
        self.clock = clock
        self.removed = set(removed)

    def fetch_article(self, url: str, last_updated: date | None) -> FetchedPage:
        self.fetched.append(url)
        if self.clock is not None:
            self.clock.now += 40.0
        if url in self.failing:
            raise FetchError(f"request for {url} failed")
        title = url.removeprefix(WIKI).replace("_", " ")
        html = REMOVED.read_text(encoding="utf-8") if url in self.removed else page_html(title)
        return FetchedPage(url, html, date(2026, 10, 13), last_updated)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SpyEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbedResult:
        self.calls.append(list(texts))
        return super().embed(texts, input_type)


class FakeWiki(FakeArticles):
    def __init__(self, failing: Sequence[str] = ()) -> None:
        super().__init__(failing)
        api = FakeApi(REVS)
        self.api_get, self.api_post = api.api_get, api.api_post


class RecordingStore(InMemoryVectorStore):
    def __init__(self, manifest_path: Path) -> None:
        super().__init__()
        self.manifest_path = manifest_path
        self.revid_at_replace: dict[str, int | None] = {}

    def replace(self, url: str, chunks: Sequence[Chunk], vectors: Sequence[list[float]]) -> None:
        entry = load_manifest(self.manifest_path).articles.get(url)
        self.revid_at_replace[url] = entry.revid if entry else None
        super().replace(url, chunks, vectors)

    def urls(self) -> set[str]:
        return set(self._by_url)


def url(title: str) -> str:
    return WIKI + title.replace(" ", "_")


def fixture_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "manifest.json"
    path.parent.mkdir(parents=True)
    shutil.copy(MANIFEST, path)
    return path


def deps_for(
    path: Path,
    fetcher: FakeArticles,
    store: InMemoryVectorStore,
    guard: CostGuard,
    embedder: FakeEmbedder | None = None,
    max_batch_tokens: int = 8000,
    clock: FakeClock | None = None,
) -> CrawlDeps:
    return CrawlDeps(
        fetcher,
        embedder or FakeEmbedder(),
        store,
        guard,
        path,
        CHUNK,
        max_batch_tokens,
        clock or FakeClock(),
    )


def new_record(plan: CrawlPlan) -> CrawlRecord:
    return CrawlRecord(
        planned=len(plan.todo), skipped=plan.unchanged, estimate_fetch_s=0, estimate_embed_s=0
    )


def planned(titles: Sequence[str]) -> CrawlPlan:
    todo = tuple(PlannedArticle(t, url(t), i + 1, date(2026, 10, 1)) for i, t in enumerate(titles))
    return CrawlPlan(todo, len(todo), 0, 0, ())


def test_criterion_1_list_articles_pages_and_filters() -> None:
    """criterion 1: "`list_articles` pages through `allpages` with `apcontinue` (two-page fixture), keeps only namespace-0 non-redirect titles, drops configured skip prefixes, and builds canonical unencoded URLs." """
    api = FakeApi(REVS)
    articles = list_articles(api.api_get, API, SKIP)
    assert [a.title for a in articles] == list(REVS)
    assert articles[0].url == "https://www.kolzchut.org.il/he/דמי_אבטלה"
    assert articles[-1].url == "https://www.kolzchut.org.il/he/קצבת_זיקנה_(קצבת_אזרח_ותיק)"
    assert len(api.queries) == 2
    first, second = api.queries
    assert "apcontinue" not in first and second["apcontinue"] == ["פנסיה"]
    for query in api.queries:
        assert query["list"] == ["allpages"] and query["apnamespace"] == ["0"]
        assert query["apfilterredir"] == ["nonredirects"] and query["aplimit"] == ["500"]


def test_criterion_2_latest_revisions_in_batches_of_50() -> None:
    """criterion 2: "`latest_revisions` batches 50 titles per request (fixture of 120 titles → 3 requests) and returns revid and timestamp per title." """
    titles = [f"ערך {i}" for i in range(120)]
    api = FakeApi({t: 1000 + i for i, t in enumerate(titles) if i != 7})
    revisions = latest_revisions(api.api_post, API, titles)
    assert api.queries == []
    assert [len(q["titles"].split("|")) for q in api.posts] == [50, 50, 20]
    assert all(q["rvprop"] == "ids|timestamp" for q in api.posts)
    assert len(revisions) == 119 and "ערך 7" not in revisions
    assert revisions["ערך 119"] == Revision(1119, datetime.fromisoformat(STAMP))


def test_latest_revisions_splits_batches_by_encoded_size() -> None:
    titles = [f"{'א' * 99} {i}" for i in range(10)]
    api = FakeApi({t: i for i, t in enumerate(titles)})
    revisions = latest_revisions(api.api_post, API, titles, max_chars=3000)
    assert [len(q["titles"].split("|")) for q in api.posts] == [5, 5]
    assert all(len(quote_plus(q["titles"])) <= 3000 for q in api.posts)
    assert len(revisions) == 10


def test_criterion_3_crawl_plan_classifies_and_estimates(cli_root: Path) -> None:
    """criterion 3: "`crawl plan` against a fixture manifest classifies articles as new, changed, unchanged and gone, and prints the counts and the time estimate from the configured delays." """
    fixture_manifest(cli_root)
    config = cli_root / "config" / "zchut.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["crawl"].update(fetch_delay_s=1200, tokens_per_article=9600)
    data["embed"].update(max_batch_tokens=8000, min_interval_s=1800)
    config.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    wiki = FakeWiki()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_wiki", lambda settings: wiki)
        result = runner.invoke(app, ["crawl", "plan"])
    assert wiki.fetched == []
    assert result.exit_code == 0, result.output
    for line in ["חדשים: 2", "השתנו: 1", "ללא שינוי: 1", "הוסרו מהאתר: 1"]:
        assert line in result.output
    # 3 articles x 1200 s fetch = 1 h; 28,800 tokens / 8,000 = 4 requests x 1800 s = 2 h.
    assert "הורדה 1.0 שעות" in result.output and "הטמעה 2.0 שעות" in result.output
    assert "בסך הכול כ-2.0 שעות" in result.output

    plan = make_plan(FakeApi(REVS), load_settings(cli_root).crawl, load_manifest(MANIFEST))
    assert [a.title for a in plan.todo] == ["החזר מס הכנסה", "פנסיה", "קצבת זיקנה (קצבת אזרח ותיק)"]
    assert plan.gone == (url("דף שנמחק"),)


def test_criterion_4_run_limit_resumes_and_skips_unchanged(tmp_path: Path, priced: Prices) -> None:
    """criterion 4: "`crawl run --limit 2` fetches, chunks, embeds and stores exactly two planned articles, writes their manifest entries only after the store replace, and a second run continues with the next articles; an unchanged article is never fetched or embedded." """
    path = fixture_manifest(tmp_path)
    store = RecordingStore(path)
    fetcher = FakeArticles()
    embedder = SpyEmbedder()
    for _ in range(2):
        manifest = load_manifest(path)
        plan = make_plan(FakeApi(REVS), load_settings(ROOT).crawl, manifest)
        record = new_record(plan)
        deps = deps_for(path, fetcher, store, make_guard(priced), embedder)
        run_crawl(plan, manifest, deps, record, limit=2)
    changed, pensia, zikna = url("החזר מס הכנסה"), url("פנסיה"), url("קצבת זיקנה (קצבת אזרח ותיק)")
    assert fetcher.fetched == [changed, pensia, zikna]
    assert store.revid_at_replace == {changed: 200, pensia: None, zikna: None}
    assert store.urls() == {changed, pensia, zikna}
    entries = load_manifest(path).articles
    assert entries[changed].revid == 201 and entries[pensia].revid == 300
    assert all(entries[u].status == "ok" and entries[u].chunks == 1 for u in store.urls())
    assert url("דמי אבטלה") not in fetcher.fetched
    assert not any("דמי אבטלה" in text for call in embedder.calls for text in call)
    assert record.planned == 1 and record.skipped == 3 and record.embedded == 1


def test_criterion_4_first_run_stops_at_the_limit(tmp_path: Path, priced: Prices) -> None:
    """criterion 4: the first `--limit 2` run stores exactly two and leaves the third for later."""
    path = fixture_manifest(tmp_path)
    manifest = load_manifest(path)
    plan = make_plan(FakeApi(REVS), load_settings(ROOT).crawl, manifest)
    record = new_record(plan)
    fetcher = FakeArticles()
    run_crawl(
        plan, manifest, deps_for(path, fetcher, RecordingStore(path), make_guard(priced)), record, 2
    )
    assert len(fetcher.fetched) == 2 and record.embedded == 2
    assert record.remaining == 1 and record.stopped_by == "limit"
    assert url("קצבת זיקנה (קצבת אזרח ותיק)") not in load_manifest(path).articles


def test_criterion_5_fetch_error_recorded_and_gone_removed(tmp_path: Path, priced: Prices) -> None:
    """criterion 5: "A fetch error on one article records `status: error` in the manifest and the run continues; a `gone` article is removed from the store and the manifest." """
    path = fixture_manifest(tmp_path)
    gone = url("דף שנמחק")
    store = InMemoryVectorStore()
    old = Chunk("old-000", gone, "דף שנמחק", "דף שנמחק", "טקסט", "2026-10-01", "CC-BY-SA-3.0", None)
    store.replace(gone, [old], [FakeEmbedder().vector("טקסט")])
    manifest = load_manifest(path)
    plan = make_plan(FakeApi(REVS), load_settings(ROOT).crawl, manifest)
    record = new_record(plan)
    pensia = url("פנסיה")
    fetcher = FakeArticles(failing=[pensia])
    run_crawl(plan, manifest, deps_for(path, fetcher, store, make_guard(priced)), record)
    entries = load_manifest(path).articles
    assert entries[pensia].status == "error" and "failed" in (entries[pensia].error or "")
    assert entries[url("קצבת זיקנה (קצבת אזרח ותיק)")].status == "ok"
    assert gone not in entries and record.deleted == 1
    assert all(hit.chunk.url != gone for hit in store.search(FakeEmbedder().vector("טקסט"), 10))
    assert (record.errors, record.embedded, record.remaining) == (1, 2, 0)
    again = make_plan(FakeApi(REVS), load_settings(ROOT).crawl, load_manifest(path))
    assert [a.url for a in again.todo] == [pensia] and again.changed == 1


def test_criterion_6_max_minutes_stops_between_articles(tmp_path: Path, priced: Prices) -> None:
    """criterion 6: "`--max-minutes` stops between articles with the manifest consistent (an injected clock), and `--max-usd` aborts before the embed call that would exceed it." """
    path = tmp_path / "manifest.json"
    plan = planned([f"ערך {i}" for i in range(5)])
    clock = FakeClock()
    fetcher = FakeArticles(clock=clock)
    store = RecordingStore(path)
    record = new_record(plan)
    deps = deps_for(path, fetcher, store, make_guard(priced), max_batch_tokens=1, clock=clock)
    run_crawl(plan, Manifest(), deps, record, max_minutes=2)
    # 40 s per fetch: articles start at 0, 40 and 80 s; at 120 s the clock says stop.
    assert len(fetcher.fetched) == 3 and record.stopped_by == "max_minutes"
    entries = load_manifest(path).articles
    assert set(entries) == store.urls() == set(fetcher.fetched)
    assert record.remaining == 2 and record.elapsed_s == 120


def test_criterion_6_max_usd_aborts_before_the_embed_call(tmp_path: Path) -> None:
    """criterion 6: `--max-usd` stops before the embed call that would cross the cap."""
    path = tmp_path / "manifest.json"
    plan = planned([f"ערך {i}" for i in range(3)])
    prices = Prices(models={FAKE_EMBED_MODEL: ModelPrice(input=1_000_000.0)})
    first = FakeArticles().fetch_article(plan.todo[0].url, None)
    chunks = chunk_page(parse_page(first), **CHUNK.model_dump())
    tokens = sum(estimate_tokens(embed_text(c)) for c in chunks)
    guard = CostGuard(tokens * 1.5, prices, RunRecorder("crawl", False, math.inf))
    embedder = SpyEmbedder()
    store = RecordingStore(path)
    record = new_record(plan)
    deps = deps_for(path, FakeArticles(), store, guard, embedder, max_batch_tokens=1)
    with pytest.raises(BudgetExceeded):
        run_crawl(plan, Manifest(), deps, record)
    assert len(embedder.calls) == 1 and store.urls() == {plan.todo[0].url}
    assert set(load_manifest(path).articles) == {plan.todo[0].url}
    assert record.stopped_by == "budget" and record.remaining == 2
    assert record.cost_usd == guard.spent <= guard.max_usd


def test_criterion_7_crawl_record_redacted(cli_root: Path) -> None:
    """criterion 7: "The crawl record stores counts, elapsed, tokens and cost and no article text or titles (redaction test)." """
    fixture_manifest(cli_root)
    prices = cli_root / "config" / "prices.json"
    data = json.loads(prices.read_text(encoding="utf-8"))
    data[FAKE_EMBED_MODEL] = {"input": 0.02}
    prices.write_text(json.dumps(data), encoding="utf-8")
    wiki = FakeWiki(failing=[url("פנסיה")])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_wiki", lambda settings: wiki)
        mp.setattr(cli, "_embedder", lambda ctx: FakeEmbedder())
        mp.setattr(cli, "ChromaStore", lambda path: InMemoryVectorStore())
        result = runner.invoke(app, ["crawl", "run", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "נשמרו: 1" in result.output and "שגיאות: 1" in result.output
    [path] = (cli_root / "runs").glob("*-crawl.json")
    text = path.read_text(encoding="utf-8")
    crawl = json.loads(text)["crawl"]
    assert (crawl["planned"], crawl["embedded"], crawl["errors"], crawl["skipped"]) == (3, 1, 1, 1)
    assert crawl["deleted"] == 1 and crawl["remaining"] == 1 and crawl["store_chunks"] == 1
    assert (crawl["new"], crawl["changed"], crawl["report_posted"]) == (2, 1, False)
    assert crawl["embed_tokens"] > 0 and crawl["cost_usd"] > 0 and crawl["elapsed_s"] >= 0
    for title in [*REVS, "דף שנמחק", "כל עובד שכיר", "kolzchut.org.il/he", "failed"]:
        assert title not in text and title.replace(" ", "_") not in text
    assert load_manifest(cli_root / "data" / "manifest.json").articles[url("פנסיה")].error


def test_removed_stub_is_skipped_and_not_replanned(tmp_path: Path, priced: Prices) -> None:
    path = fixture_manifest(tmp_path)
    pensia = url("פנסיה")
    manifest = load_manifest(path)
    settings = load_settings(ROOT)
    plan = make_plan(FakeApi(REVS), settings.crawl, manifest)
    record = new_record(plan)
    store = RecordingStore(path)
    embedder = SpyEmbedder()
    fetcher = FakeArticles(removed=[pensia])
    run_crawl(plan, manifest, deps_for(path, fetcher, store, make_guard(priced), embedder), record)
    entry = load_manifest(path).articles[pensia]
    assert (entry.status, entry.reason, entry.revid, entry.chunks) == ("skipped", "removed", 300, 0)
    assert entry.error is None and pensia not in store.urls()
    assert not any("מיזם" in text for call in embedder.calls for text in call)
    assert url("קצבת זיקנה (קצבת אזרח ותיק)") in store.urls()
    assert (record.empty, record.errors, record.embedded, record.remaining) == (1, 0, 2, 0)
    again = make_plan(FakeApi(REVS), settings.crawl, load_manifest(path))
    assert pensia not in [a.url for a in again.todo]
    assert (again.unchanged, again.skipped) == (4, 1)
    est = estimate(len(again.todo), settings.crawl, settings.embed)
    assert "דפים ריקים שדולגו: 1" in format_plan(again, est)
    bumped = make_plan(FakeApi({**REVS, "פנסיה": 301}), settings.crawl, load_manifest(path))
    assert [a.url for a in bumped.todo] == [pensia] and bumped.skipped == 0
