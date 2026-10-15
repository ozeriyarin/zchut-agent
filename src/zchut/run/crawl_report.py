from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from zchut.answer.prompt import format_date
from zchut.index.manifest import Manifest
from zchut.sources.mediawiki import PAGE_PREFIX
from zchut.types import CrawlRecord, RunRecord

RECENT_TITLES = 10


@dataclass(frozen=True)
class CrawlRun:
    path: Path
    record: RunRecord
    crawl: CrawlRecord


def crawl_runs(runs_dir: Path) -> list[CrawlRun]:
    found: list[CrawlRun] = []
    for path in runs_dir.glob("*-crawl*.json"):
        try:
            record = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            continue
        if record.crawl is not None:
            found.append(CrawlRun(path, record, record.crawl))
    return sorted(found, key=lambda r: (r.record.started_at, r.path.name))


def mark_posted(run: CrawlRun) -> None:
    run.crawl.report_posted = True
    run.path.write_text(run.record.model_dump_json(indent=2), encoding="utf-8")


def _title(url: str) -> str:
    return unquote(urlsplit(url).path.removeprefix(PAGE_PREFIX)).replace("_", " ")


def recent_titles(manifest: Manifest, limit: int = RECENT_TITLES) -> list[tuple[str, str]]:
    dated = [
        (entry.last_updated, url)
        for url, entry in manifest.articles.items()
        if entry.status == "ok" and entry.last_updated
    ]
    return [(_title(url), day) for day, url in sorted(dated, reverse=True)[:limit]]


def format_report(latest: CrawlRun, previous: CrawlRun | None, manifest: Manifest) -> str:
    crawl = latest.crawl
    since = f" (הקודמת: {previous.record.started_at})" if previous else " (אין סריקה קודמת)"
    delta = crawl.store_chunks - previous.crawl.store_chunks if previous else 0
    statuses = [e.status for e in manifest.articles.values()]
    lines = [
        f"דוח סריקה שבועי: {latest.record.started_at}{since}",
        f"חדשים: {crawl.new}",
        f"השתנו: {crawl.changed}",
        f"נשמרו: {crawl.embedded}",
        f"הוסרו מהאתר: {crawl.deleted}",
        f"ללא שינוי: {crawl.skipped}",
        f"דפים ריקים שדולגו: {statuses.count('skipped')}",
        f"שגיאות: {crawl.errors}",
        f"נותרו: {crawl.remaining}",
        f"מאמרים באינדקס: {statuses.count('ok')}",
        f"מקטעים באינדקס: {crawl.store_chunks} ({delta:+d})",
        f"זמן: {crawl.elapsed_s / 60:.1f} דקות",
        f"טוקנים: {crawl.embed_tokens:,}",
        f"עלות: {crawl.cost_usd:.4f}$",
        f"עצירה: {crawl.stopped_by}",
        "עודכנו לאחרונה:",
    ]
    lines += [f"  - {title} ({format_date(day)})" for title, day in recent_titles(manifest)]
    return "\n".join(lines)
