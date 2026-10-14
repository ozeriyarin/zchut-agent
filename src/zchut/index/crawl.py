from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from zchut.config import ChunkSettings, CrawlSettings, EmbedSettings
from zchut.index.embedder import Embedder, EmbedError
from zchut.index.ingest import embed_text
from zchut.index.manifest import Manifest, ManifestEntry, save_manifest
from zchut.index.store import VectorStore
from zchut.run.cost_guard import BudgetExceeded, CostGuard
from zchut.sources.catalog import Article, CatalogApi, Revision, latest_revisions, list_articles
from zchut.sources.chunk import chunk_page
from zchut.sources.fetch import FetchError
from zchut.sources.parse import EmptyPage, ParseError, parse_page
from zchut.tokens import estimate_tokens
from zchut.types import Chunk, CrawlRecord, FetchedPage


class ArticleFetcher(Protocol):
    def fetch_article(self, url: str, last_updated: date | None) -> FetchedPage: ...


@dataclass(frozen=True)
class PlannedArticle:
    title: str
    url: str
    revid: int
    last_updated: date


@dataclass(frozen=True)
class CrawlPlan:
    todo: tuple[PlannedArticle, ...]
    new: int
    changed: int
    unchanged: int
    gone: tuple[str, ...]
    skipped: int = 0


@dataclass(frozen=True)
class Estimate:
    tokens: int
    fetch_s: float
    embed_s: float


@dataclass(frozen=True)
class CrawlDeps:
    fetcher: ArticleFetcher
    embedder: Embedder
    store: VectorStore
    guard: CostGuard
    manifest_path: Path
    chunk: ChunkSettings
    max_batch_tokens: int
    clock: Callable[[], float] = time.monotonic


@dataclass
class _Batch:
    items: list[tuple[PlannedArticle, list[Chunk], date]] = field(default_factory=list)
    tokens: int = 0


def diff(
    articles: Sequence[Article], revisions: Mapping[str, Revision], manifest: Manifest
) -> CrawlPlan:
    todo: list[PlannedArticle] = []
    listed: set[str] = set()
    new = unchanged = skipped = 0
    for article in articles:
        listed.add(article.url)
        rev = revisions.get(article.title)
        if rev is None:
            continue
        entry = manifest.articles.get(article.url)
        if entry is not None and entry.status != "error" and entry.revid == rev.revid:
            unchanged += 1
            skipped += entry.status == "skipped"
            continue
        if entry is None:
            new += 1
        todo.append(PlannedArticle(article.title, article.url, rev.revid, rev.timestamp.date()))
    gone = tuple(sorted(url for url in manifest.articles if url not in listed))
    todo.sort(key=lambda a: a.title)
    return CrawlPlan(tuple(todo), new, len(todo) - new, unchanged, gone, skipped)


def make_plan(api: CatalogApi, crawl: CrawlSettings, manifest: Manifest) -> CrawlPlan:
    articles = list_articles(api.api_get, crawl.api_url, crawl.skip_prefixes, crawl.list_batch)
    titles = [a.title for a in articles]
    revisions = latest_revisions(
        api.api_post, crawl.api_url, titles, crawl.revisions_batch, crawl.max_query_chars
    )
    return diff(articles, revisions, manifest)


def estimate(articles: int, crawl: CrawlSettings, embed: EmbedSettings) -> Estimate:
    tokens = articles * crawl.tokens_per_article
    requests = math.ceil(tokens / embed.max_batch_tokens)
    return Estimate(tokens, articles * crawl.fetch_delay_s, requests * embed.min_interval_s)


def format_plan(plan: CrawlPlan, est: Estimate) -> str:
    # Embedding is paced while the next pages are fetched, so the two overlap.
    total_h = max(est.fetch_s, est.embed_s) / 3600
    return "\n".join(
        [
            f"חדשים: {plan.new}",
            f"השתנו: {plan.changed}",
            f"ללא שינוי: {plan.unchanged}",
            f"דפים ריקים שדולגו: {plan.skipped}",
            f"הוסרו מהאתר: {len(plan.gone)}",
            f"טוקנים משוערים: {est.tokens:,}",
            f"הערכת זמן: הורדה {est.fetch_s / 3600:.1f} שעות, "
            f"הטמעה {est.embed_s / 3600:.1f} שעות, בסך הכול כ-{total_h:.1f} שעות",
        ]
    )


def format_run(record: CrawlRecord) -> str:
    return "\n".join(
        [
            f"מתוכננים: {record.planned}",
            f"נשמרו: {record.embedded}",
            f"שגיאות: {record.errors}",
            f"דפים ריקים שדולגו: {record.empty}",
            f"ללא שינוי: {record.skipped}",
            f"נמחקו: {record.deleted}",
            f"נותרו: {record.remaining}",
            f"מקטעים באינדקס: {record.store_chunks}",
            f"עצירה: {record.stopped_by}",
        ]
    )


def _mark(manifest: Manifest, article: PlannedArticle, entry: ManifestEntry, path: Path) -> None:
    manifest.articles[article.url] = entry
    save_manifest(path, manifest)


def _error(article: PlannedArticle, exc: Exception) -> ManifestEntry:
    empty = exc if isinstance(exc, EmptyPage) else None
    return ManifestEntry(
        revid=article.revid,
        last_updated=article.last_updated.isoformat(),
        fetched_at=date.today().isoformat(),
        chunks=0,
        status="error" if empty is None else "skipped",
        error=str(exc) if empty is None else None,
        reason=None if empty is None else empty.reason,
    )


def _flush(batch: _Batch, manifest: Manifest, deps: CrawlDeps, record: CrawlRecord) -> None:
    items, batch.items, batch.tokens = batch.items, [], 0
    texts = [embed_text(c) for _, chunks, _ in items for c in chunks]
    try:
        vectors = deps.guard.embed(deps.embedder, texts, "document") if texts else []
    except EmbedError as exc:
        for article, _, _ in items:
            _mark(manifest, article, _error(article, exc), deps.manifest_path)
            record.errors += 1
        return
    offset = 0
    for article, chunks, fetched_at in items:
        deps.store.replace(article.url, chunks, vectors[offset : offset + len(chunks)])
        offset += len(chunks)
        entry = ManifestEntry(
            revid=article.revid,
            last_updated=article.last_updated.isoformat(),
            fetched_at=fetched_at.isoformat(),
            chunks=len(chunks),
            status="ok",
        )
        _mark(manifest, article, entry, deps.manifest_path)
        record.embedded += 1


def run_crawl(
    plan: CrawlPlan,
    manifest: Manifest,
    deps: CrawlDeps,
    record: CrawlRecord,
    limit: int | None = None,
    max_minutes: float | None = None,
) -> None:
    started = deps.clock()
    for url in plan.gone:
        deps.store.delete(url)
        manifest.articles.pop(url, None)
        record.deleted += 1
    if plan.gone:
        save_manifest(deps.manifest_path, manifest)
    batch = _Batch()
    try:
        for article in plan.todo[:limit]:
            if max_minutes is not None and deps.clock() - started >= max_minutes * 60:
                record.stopped_by = "max_minutes"
                break
            try:
                fetched = deps.fetcher.fetch_article(article.url, article.last_updated)
                chunks = chunk_page(parse_page(fetched), **deps.chunk.model_dump())
            except (FetchError, ParseError) as exc:
                _mark(manifest, article, _error(article, exc), deps.manifest_path)
                if isinstance(exc, EmptyPage):
                    record.empty += 1
                else:
                    record.errors += 1
                continue
            record.fetched += 1
            batch.items.append((article, chunks, fetched.fetched_at))
            batch.tokens += sum(estimate_tokens(embed_text(c)) for c in chunks)
            if batch.tokens >= deps.max_batch_tokens:
                _flush(batch, manifest, deps, record)
        _flush(batch, manifest, deps, record)
    except BudgetExceeded:
        record.stopped_by = "budget"
        raise
    finally:
        record.remaining = len(plan.todo) - record.embedded - record.errors - record.empty
        if record.stopped_by == "done" and record.remaining:
            record.stopped_by = "limit"
        record.store_chunks = deps.store.count()
        record.elapsed_s = deps.clock() - started
        calls = deps.guard.recorder.record.calls
        record.embed_tokens = sum(c.input_tokens for c in calls if c.role == "embed")
        record.cost_usd = deps.guard.spent
