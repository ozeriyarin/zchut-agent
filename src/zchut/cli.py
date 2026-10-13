from __future__ import annotations

import math
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import anthropic
import click
import httpx
import typer

from zchut.answer.model import FAKE_ANSWER_MODEL, AnswerModel, FakeAnswerModel, make_haiku_client
from zchut.answer.tiers import TIERS, VERIFIERS, make_answer_model, parse_choices
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
from zchut.eval.run_eval import format_table, run_eval
from zchut.graph.build import GraphDeps, Reply
from zchut.graph.classify import FAKE_CLASSIFIER_MODEL, Classifier, FakeClassifier, HaikuClassifier
from zchut.graph.offices import load_offices
from zchut.graph.state import HISTORY_CAP
from zchut.graph.verify import (
    FAKE_VERIFIER_MODEL,
    FakeVerifier,
    HaikuVerifier,
    JevVerifier,
    Verifier,
)
from zchut.index.embedder import FAKE_EMBED_MODEL, Embedder, FakeEmbedder, VoyageEmbedder
from zchut.index.ingest import ingest as ingest_pages
from zchut.index.store import ChromaStore, InMemoryVectorStore, VectorStore
from zchut.run.ask import AskDeps
from zchut.run.converse import run_conversation
from zchut.run.cost_guard import CostGuard, Outcome, guarded
from zchut.run.cost_report import format_cost, summarize_runs
from zchut.run.records import RunRecorder
from zchut.sources.fetch import Fetcher, FetcherRouter, FixtureFetcher, HttpxFetcher
from zchut.sources.mediawiki import MediaWikiFetcher, urllib_opener
from zchut.types import IngestReport

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Zchut: Hebrew RAG over public pages."
)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

DryRun = Annotated[bool, typer.Option("--dry-run", help="Use the fakes; no network, no keys.")]
MaxUsd = Annotated[
    float | None, typer.Option("--max-usd", help="Cost cap in USD (default from config).")
]
K = Annotated[int | None, typer.Option("--k", min=1, help="Chunks to retrieve.")]
TierOpt = Annotated[
    str, typer.Option("--tier", click_type=click.Choice(TIERS), help="Answer model tier.")
]
VerifierOpt = Annotated[
    str, typer.Option("--verifier", click_type=click.Choice(VERIFIERS), help="Verifier tier.")
]
NoVerify = Annotated[bool, typer.Option("--no-verify", help="Skip the verify step.")]


@dataclass(frozen=True)
class Context:
    settings: Settings
    prices: Prices
    secrets: Secrets
    recorder: RunRecorder
    guard: CostGuard
    dry_run: bool


def _dotenv_path() -> Path:
    return PACKAGE_ROOT / ".env"


def _context(command: str, dry_run: bool, max_usd: float | None) -> Context:
    root = Path.cwd()
    settings = load_settings(root)
    prices = load_prices(root)
    if dry_run:
        fakes = [FAKE_EMBED_MODEL, FAKE_ANSWER_MODEL, FAKE_CLASSIFIER_MODEL, FAKE_VERIFIER_MODEL]
        prices = prices.with_free(fakes)
    cap = settings.max_usd if max_usd is None else max_usd
    recorder = RunRecorder(command, dry_run, cap)
    guard = CostGuard(cap, prices, recorder)
    return Context(settings, prices, load_secrets(_dotenv_path()), recorder, guard, dry_run)


def _embedder(ctx: Context) -> Embedder:
    if ctx.dry_run:
        return FakeEmbedder()
    embed = ctx.settings.embed
    return VoyageEmbedder(
        ctx.secrets.require_voyage(), ctx.settings.models.embed, **embed.model_dump()
    )


def _answer_model(ctx: Context, tier: str) -> AnswerModel:
    if ctx.dry_run:
        return FakeAnswerModel(max_tokens=ctx.settings.answer.max_tokens)
    return make_answer_model(tier, ctx.secrets, ctx.settings)


def _haiku(ctx: Context) -> anthropic.Anthropic:
    return make_haiku_client(ctx.secrets.require_anthropic(), ctx.settings.answer.timeout_s)


def _classifier(ctx: Context) -> Classifier:
    max_tokens = ctx.settings.graph.classify_max_tokens
    if ctx.dry_run:
        return FakeClassifier(max_tokens)
    return HaikuClassifier(_haiku(ctx), ctx.settings.models.answer, max_tokens)


def _verifier(ctx: Context, tier: str | None) -> Verifier | None:
    graph = ctx.settings.graph
    if tier is None:
        return None
    if ctx.dry_run:
        return FakeVerifier(tier, graph.verify_max_tokens)
    haiku = HaikuVerifier(_haiku(ctx), ctx.settings.models.answer, graph.verify_max_tokens)
    if tier == "haiku":
        return haiku
    http = httpx.Client(timeout=ctx.settings.answer.timeout_s)
    key = ctx.secrets.require_openrouter()
    return JevVerifier(http, key, ctx.settings.models.jev, graph.jev, haiku)


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


def _live_fetcher(settings: Settings) -> FetcherRouter:
    fetch, raw_dir = settings.fetch, settings.paths.raw_dir
    client = httpx.Client(timeout=fetch.timeout_s)
    opener = urllib_opener(fetch.timeout_s)
    wiki = MediaWikiFetcher(raw_dir, fetch.user_agent, fetch.min_interval_s, opener)
    web = HttpxFetcher(client, raw_dir, fetch.user_agent, fetch.min_interval_s)
    return FetcherRouter({"kolzchut.org.il": wiki}, web)


def _store(ctx: Context, embedder: Embedder) -> VectorStore:
    if not ctx.dry_run:
        return ChromaStore(ctx.settings.paths.chroma_dir)
    store = InMemoryVectorStore()
    fixtures = FixtureFetcher(ctx.settings.paths.fixtures_dir)
    setup = CostGuard(math.inf, ctx.prices, RunRecorder("setup", True, math.inf))
    _ingest(ctx, fixtures, fixtures.urls(), embedder, store, setup)
    return store


def _deps(ctx: Context, k: int | None) -> AskDeps:
    embedder = _embedder(ctx)
    retrieval = ctx.settings.retrieval
    # The answer model is set per tier in _graph_deps; retrieval-only evals never call it.
    model = FakeAnswerModel()
    return AskDeps(embedder, _store(ctx, embedder), model, k or retrieval.k, retrieval.min_score)


def _graph_deps(ctx: Context, ask: AskDeps, tier: str, verifier: str | None) -> GraphDeps:
    return GraphDeps(
        replace(ask, model=_answer_model(ctx, tier)),
        _classifier(ctx),
        _verifier(ctx, verifier),
        load_offices(ctx.settings.paths.offices),
        ask.k * ctx.settings.chunk.max_chars,
        tier,
    )


def _turn(ctx: Context) -> Context:
    recorder = RunRecorder(ctx.recorder.record.command, ctx.dry_run, ctx.guard.max_usd)
    return replace(ctx, recorder=recorder, guard=CostGuard(ctx.guard.max_usd, ctx.prices, recorder))


def _reply(said: list[str], fixed: str | None = None) -> Reply:
    def answer(question: str) -> str:
        if fixed is None:
            text: str = typer.prompt(question, default="", show_default=False)
        else:
            typer.echo(question)
            text = fixed
        said.append(text)
        return text

    return answer


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
        fetcher, urls = _live_fetcher(ctx.settings), ctx.settings.sources
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
    k: K = None,
    tier: TierOpt = "haiku",
    verifier: VerifierOpt = "haiku",
    no_verify: NoVerify = False,
    reply: Annotated[
        str | None, typer.Option("--reply", help="Answer to a clarification question.")
    ] = None,
    dry_run: DryRun = False,
    max_usd: MaxUsd = None,
) -> None:
    """Answer one question through the conversation graph, or refuse or escalate."""
    with _config_errors():
        ctx = _context("ask", dry_run, max_usd)
        deps = _graph_deps(ctx, _deps(ctx, k), tier, None if no_verify else verifier)
    said = [question]
    outcome = run_conversation(question, [], deps, ctx.guard, _reply(said, reply))
    _finish(ctx, outcome, said)
    raise typer.Exit(outcome.exit_code)


@app.command()
def chat(
    k: K = None,
    tier: TierOpt = "haiku",
    verifier: VerifierOpt = "haiku",
    no_verify: NoVerify = False,
    dry_run: DryRun = False,
    max_usd: MaxUsd = None,
) -> None:
    """Ask several questions in one session; an empty line ends it."""
    with _config_errors():
        ctx = _context("chat", dry_run, max_usd)
        deps = _graph_deps(ctx, _deps(ctx, k), tier, None if no_verify else verifier)
    history: list[str] = []
    while question := typer.prompt("שאלה", default="", show_default=False).strip():
        turn, said = _turn(ctx), [question]
        outcome = run_conversation(question, history, deps, turn.guard, _reply(said))
        _finish(turn, outcome, [*history, *said])
        history = [*history, *said][-HISTORY_CAP:]


@app.command("eval")
def eval_(
    retrieval_only: Annotated[
        bool, typer.Option("--retrieval-only", help="Skip the answer step.")
    ] = False,
    tier: TierOpt = "haiku",
    tiers: Annotated[
        str | None, typer.Option("--tiers", help="Comma-separated answer tiers to compare.")
    ] = None,
    verifiers: Annotated[
        str, typer.Option("--verifiers", help="Comma-separated verifier tiers to compare.")
    ] = "haiku",
    no_verify: NoVerify = False,
    dry_run: DryRun = False,
    max_usd: MaxUsd = None,
) -> None:
    """Score the 30-question set per tier and verifier, with a side-by-side table."""
    with _config_errors():
        ctx = _context("eval", dry_run, max_usd)
        questions = load_questions(ctx.settings.paths.questions)
        deps = _deps(ctx, None)
        names = parse_choices(tiers, TIERS) if tiers else [tier]
        checks: list[str | None] = [None] if no_verify else [*parse_choices(verifiers, VERIFIERS)]
        combos = (
            [] if retrieval_only else [_graph_deps(ctx, deps, t, v) for t in names for v in checks]
        )
    outcome, reports = run_eval(questions, deps, ctx.guard, combos)
    path = _finish(ctx, outcome, [])
    for report in reports:
        suffix = f"-{report.tier}-{report.verifier or 'none'}" if len(reports) > 1 else ""
        report_path = path.with_name(f"{path.stem}{suffix}-report.json")
        report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(f"דוח הערכה: {report_path}")
    if combos:
        table_path = path.with_name(f"{path.stem}-comparison.md")
        table_path.write_text(format_table(reports) + "\n", encoding="utf-8")
        typer.echo(f"טבלת השוואה: {table_path}")
    raise typer.Exit(outcome.exit_code)


@app.command()
def cost(
    days: Annotated[int, typer.Option("--days", min=1, help="How many days back to sum.")] = 7,
) -> None:
    """Sum the run records under runs/ by command and model; makes no calls, writes nothing."""
    with _config_errors():
        settings = load_settings(Path.cwd())
    since = datetime.now() - timedelta(days=days)
    typer.echo(format_cost(summarize_runs(settings.paths.runs_dir, since)))
