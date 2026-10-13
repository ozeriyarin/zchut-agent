from __future__ import annotations

import math
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
import typer

from zchut.answer.model import (
    FAKE_ANSWER_MODEL,
    AnswerModel,
    FakeAnswerModel,
    HaikuAnswerModel,
    make_haiku_client,
)
from zchut.config import (
    ConfigError,
    Prices,
    Secrets,
    Settings,
    load_prices,
    load_secrets,
    load_settings,
)
from zchut.eval.questions import load_questions
from zchut.eval.run_eval import run_eval
from zchut.index.embedder import FAKE_EMBED_MODEL, Embedder, FakeEmbedder, VoyageEmbedder
from zchut.index.ingest import ingest as ingest_pages
from zchut.index.store import ChromaStore, InMemoryVectorStore, VectorStore
from zchut.run.ask import AskDeps, run_ask
from zchut.run.cost_guard import CostGuard, Outcome, guarded
from zchut.run.records import RunRecorder
from zchut.sources.fetch import Fetcher, FixtureFetcher, HttpxFetcher
from zchut.types import IngestReport

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Zchut: Hebrew RAG over public pages."
)

DryRun = Annotated[bool, typer.Option("--dry-run", help="Use the fakes; no network, no keys.")]
MaxUsd = Annotated[
    float | None, typer.Option("--max-usd", help="Cost cap in USD (default from config).")
]


@dataclass(frozen=True)
class Context:
    settings: Settings
    prices: Prices
    secrets: Secrets
    recorder: RunRecorder
    guard: CostGuard
    dry_run: bool


def _context(command: str, dry_run: bool, max_usd: float | None) -> Context:
    root = Path.cwd()
    settings = load_settings(root)
    prices = load_prices(root)
    if dry_run:
        prices = prices.with_free([FAKE_EMBED_MODEL, FAKE_ANSWER_MODEL])
    cap = settings.max_usd if max_usd is None else max_usd
    recorder = RunRecorder(command, dry_run, cap)
    guard = CostGuard(cap, prices, recorder)
    return Context(settings, prices, load_secrets(), recorder, guard, dry_run)


def _embedder(ctx: Context) -> Embedder:
    if ctx.dry_run:
        return FakeEmbedder()
    return VoyageEmbedder(ctx.secrets.require_voyage(), ctx.settings.models.embed)


def _answer_model(ctx: Context) -> AnswerModel:
    answer = ctx.settings.answer
    if ctx.dry_run:
        return FakeAnswerModel(max_tokens=answer.max_tokens)
    client = make_haiku_client(ctx.secrets.require_anthropic(), answer.timeout_s)
    return HaikuAnswerModel(client, ctx.settings.models.answer, answer.max_tokens)


def _ingest(
    ctx: Context,
    fetcher: Fetcher,
    urls: list[str],
    embedder: Embedder,
    store: VectorStore,
    guard: CostGuard,
) -> IngestReport:
    chunk = ctx.settings.chunk
    return ingest_pages(urls, fetcher, embedder, store, guard, chunk.max_chars, chunk.min_chars)


def _store(ctx: Context, embedder: Embedder) -> VectorStore:
    if not ctx.dry_run:
        return ChromaStore(ctx.settings.paths.chroma_dir)
    store = InMemoryVectorStore()
    fixtures = FixtureFetcher(ctx.settings.paths.fixtures_dir)
    setup = CostGuard(math.inf, ctx.prices, RunRecorder("setup", True, math.inf))
    _ingest(ctx, fixtures, fixtures.urls(), embedder, store, setup)
    return store


def _deps(ctx: Context, k: int | None, answers: bool = True) -> AskDeps:
    embedder = _embedder(ctx)
    retrieval = ctx.settings.retrieval
    # Retrieval-only evals never call the answer model, so they need no Anthropic key.
    model = _answer_model(ctx) if answers else FakeAnswerModel()
    return AskDeps(embedder, _store(ctx, embedder), model, k or retrieval.k, retrieval.min_score)


def _finish(ctx: Context, outcome: Outcome, redact: list[str]) -> Path:
    path = ctx.recorder.write(ctx.settings.paths.runs_dir, [*ctx.secrets.values(), *redact])
    typer.echo(outcome.output, err=outcome.exit_code != 0)
    typer.echo(f"רשומת ריצה: {path}")
    return path


@contextmanager
def _config_errors() -> Iterator[None]:
    try:
        yield
    except ConfigError as exc:
        typer.echo(f"שגיאת תצורה: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.callback()
def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


@app.command()
def ingest(
    from_fixtures: Annotated[
        Path | None,
        typer.Option(
            "--from-fixtures", exists=True, file_okay=False, help="Index saved pages from a folder."
        ),
    ] = None,
    dry_run: DryRun = False,
    max_usd: MaxUsd = None,
) -> None:
    """Fetch, chunk and index the configured pages (or saved fixtures)."""
    with _config_errors():
        ctx = _context("ingest", dry_run, max_usd)
        embedder = _embedder(ctx)
        store = InMemoryVectorStore() if dry_run else ChromaStore(ctx.settings.paths.chroma_dir)
    fetcher: Fetcher
    if from_fixtures is not None or dry_run:
        fixtures = FixtureFetcher(from_fixtures or ctx.settings.paths.fixtures_dir)
        fetcher, urls = fixtures, fixtures.urls()
    else:
        fetch = ctx.settings.fetch
        client = httpx.Client(timeout=fetch.timeout_s)
        fetcher = HttpxFetcher(
            client, ctx.settings.paths.raw_dir, fetch.user_agent, fetch.min_interval_s
        )
        urls = ctx.settings.sources
    record = ctx.recorder.record

    def action() -> str:
        report = _ingest(ctx, fetcher, urls, embedder, store, ctx.guard)
        record.pages_indexed, record.chunks_indexed = report.pages, report.chunks
        record.errors.extend(report.errors)
        lines = [
            f"עמודים: {report.pages}",
            f"מקטעים: {report.chunks}",
            f"שגיאות: {len(report.errors)}",
        ]
        return "\n".join([*lines, *(f"  - {e}" for e in report.errors)])

    outcome = guarded(ctx.recorder, action)
    _finish(ctx, outcome, [])
    raise typer.Exit(outcome.exit_code)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="The question, in Hebrew.")],
    k: Annotated[int | None, typer.Option("--k", min=1, help="Chunks to retrieve.")] = None,
    dry_run: DryRun = False,
    max_usd: MaxUsd = None,
) -> None:
    """Answer one question with numbered citations, or refuse."""
    with _config_errors():
        ctx = _context("ask", dry_run, max_usd)
        deps = _deps(ctx, k)
    outcome = run_ask(question, deps, ctx.guard)
    _finish(ctx, outcome, [question])
    raise typer.Exit(outcome.exit_code)


@app.command("eval")
def eval_(
    retrieval_only: Annotated[
        bool, typer.Option("--retrieval-only", help="Skip the answer step.")
    ] = False,
    dry_run: DryRun = False,
    max_usd: MaxUsd = None,
) -> None:
    """Score the 30-question set: hit@3, citation precision, refusal rate."""
    with _config_errors():
        ctx = _context("eval", dry_run, max_usd)
        questions = load_questions(ctx.settings.paths.questions)
        deps = _deps(ctx, None, answers=not retrieval_only)
    outcome, report = run_eval(questions, deps, ctx.guard, retrieval_only)
    path = _finish(ctx, outcome, [])
    report_path = path.with_name(f"{path.stem}-report.json")
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(f"דוח הערכה: {report_path}")
    raise typer.Exit(outcome.exit_code)
