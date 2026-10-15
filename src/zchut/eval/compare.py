from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import fmean

from pydantic import BaseModel

from zchut.answer.model import FakeAnswerModel
from zchut.config import ChunkEngine, ChunkSettings, EmbedEngine
from zchut.eval.questions import EvalQuestion
from zchut.eval.run_eval import retrieval_report, retrieve_questions
from zchut.index.embedder import Embedder
from zchut.index.ingest import ingest
from zchut.index.llama_chunker import make_chunker
from zchut.index.store import VectorStore
from zchut.run.ask import AskDeps
from zchut.run.cost_guard import CostGuard
from zchut.sources.fetch import Fetcher
from zchut.types import FetchedPage

BEST_SCORE_SLACK = 0.02


@dataclass(frozen=True)
class Combo:
    chunk: ChunkEngine
    embed: EmbedEngine

    @property
    def name(self) -> str:
        return f"{self.chunk} + {self.embed}"

    @property
    def collection(self) -> str:
        return f"cmp-{self.chunk}-{self.embed}"


# The first combination is the baseline every other row is judged against.
COMBOS = (
    Combo("section", "httpx"),
    Combo("llama-markdown", "httpx"),
    Combo("llama-sentence", "httpx"),
    Combo("section", "llama"),
)


class CompareRow(BaseModel):
    engine: str
    chunks: int
    mean_chunk_chars: float
    hit_at_3: float | None
    mean_best_score: float | None
    embed_tokens: int
    cost_usd: float
    cost_estimated: bool
    elapsed_s: float
    errors: list[str] = []


class CompareReport(BaseModel):
    date: str
    dry_run: bool
    pages: int
    questions: int
    rows: list[CompareRow]
    recommendation: str


@dataclass(frozen=True)
class CompareDeps:
    urls: Sequence[str]
    fetcher: Fetcher
    questions: Sequence[EvalQuestion]
    chunk: ChunkSettings
    embedders: Mapping[EmbedEngine, Embedder]
    store_for: Callable[[Combo], VectorStore]
    k: int
    min_score: float


class MemoFetcher:
    def __init__(self, inner: Fetcher) -> None:
        self._inner = inner
        self._pages: dict[str, FetchedPage] = {}

    def fetch_page(self, url: str) -> FetchedPage:
        if url not in self._pages:
            self._pages[url] = self._inner.fetch_page(url)
        return self._pages[url]


def run_row(combo: Combo, deps: CompareDeps, guard: CostGuard) -> CompareRow:
    record = guard.recorder.record
    before, started = len(record.calls), time.perf_counter()
    store, embedder, chunk = deps.store_for(combo), deps.embedders[combo.embed], deps.chunk
    chunker = make_chunker(chunk, combo.chunk)
    indexed = ingest(
        deps.urls, deps.fetcher, embedder, store, guard, chunk.max_chars, chunk.min_chars, chunker
    )
    ask = AskDeps(embedder, store, FakeAnswerModel(), deps.k, deps.min_score)
    scored = retrieval_report(retrieve_questions(deps.questions, ask, guard), deps.min_score)
    calls = record.calls[before:]
    lengths = [len(c.text) for c in store.chunks()]
    best = [
        r.best_score
        for r in scored.rows
        if r.expected_urls and not r.expect_refusal and r.best_score is not None
    ]
    return CompareRow(
        engine=combo.name,
        chunks=len(lengths),
        mean_chunk_chars=fmean(lengths) if lengths else 0.0,
        hit_at_3=scored.hit_at_3,
        mean_best_score=fmean(best) if best else None,
        embed_tokens=sum(c.input_tokens for c in calls),
        cost_usd=sum(c.cost_usd for c in calls),
        cost_estimated=any(c.cost_estimated for c in calls),
        elapsed_s=time.perf_counter() - started,
        errors=list(indexed.errors),
    )


def _qualifies(row: CompareRow, base: CompareRow) -> bool:
    hit, score, base_hit, base_score = (
        row.hit_at_3,
        row.mean_best_score,
        base.hit_at_3,
        base.mean_best_score,
    )
    if hit is None or score is None or base_hit is None or base_score is None:
        return False
    return hit >= base_hit and score >= base_score - BEST_SCORE_SLACK


def recommend(rows: Sequence[CompareRow]) -> str:
    base, *others = rows
    rule = f"hit@3 ≥ בסיס וציון מיטבי ממוצע ≥ בסיס − {BEST_SCORE_SLACK}"
    winners = [r for r in others if _qualifies(r, base)]
    if not winners:
        return f"המלצה: להשאיר את {base.engine}; אף מנוע לא עמד בכלל ({rule})."
    best = max(winners, key=lambda r: (r.hit_at_3 or 0.0, r.mean_best_score or 0.0))
    return (
        f"המלצה: לאמץ את {best.engine}: hit@3 {best.hit_at_3:.2f} מול {base.hit_at_3:.2f}, "
        f"ציון מיטבי ממוצע {best.mean_best_score:.3f} מול {base.mean_best_score:.3f} ({rule}); "
        "ההחלפה ידנית ב-chunk.engine / embed.engine, שום דבר לא מתחלף לבד."
    )


def run_compare(deps: CompareDeps, guard: CostGuard, dry_run: bool) -> CompareReport:
    rows = [run_row(combo, deps, guard) for combo in COMBOS]
    return CompareReport(
        date=date.today().isoformat(),
        dry_run=dry_run,
        pages=len(deps.urls),
        questions=len(deps.questions),
        rows=rows,
        recommendation=recommend(rows),
    )


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def format_compare(rows: Sequence[CompareRow]) -> str:
    lines = [
        "| engine | chunks | mean chunk chars | hit@3 | mean best score | embed tokens"
        " | cost (USD) | elapsed (s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        cost = f"{r.cost_usd:.5f}" + (" (est.)" if r.cost_estimated else "")
        cells = [
            r.engine,
            str(r.chunks),
            f"{r.mean_chunk_chars:.0f}",
            _fmt(r.hit_at_3),
            _fmt(r.mean_best_score),
            str(r.embed_tokens),
            cost,
            f"{r.elapsed_s:.1f}",
        ]
        lines.append(f"| {' | '.join(cells)} |")
    return "\n".join(lines)


def write_report(report: CompareReport, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"compare-{report.date}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path
