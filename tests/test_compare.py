from __future__ import annotations

import json
from dataclasses import fields
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from llama_index.embeddings.voyageai import VoyageEmbedding
from typer.testing import CliRunner
from voyageai.error import AuthenticationError, RateLimitError

from conftest import AVTALA, FIXTURES, make_guard
from zchut import cli
from zchut.cli import app
from zchut.config import Prices, Settings
from zchut.eval.compare import CompareReport, CompareRow, recommend
from zchut.index.embedder import Embedder, EmbedError, FakeEmbedder, VoyageEmbedder
from zchut.index.llama_chunker import LlamaChunker, make_chunker
from zchut.index.llama_embedder import LlamaVoyageEmbedder
from zchut.index.store import ChromaStore
from zchut.sources.chunk import PATH_SEP, chunk_page
from zchut.sources.fetch import FixtureFetcher
from zchut.sources.parse import parse_page
from zchut.tokens import estimate_tokens
from zchut.types import Chunk, Page

runner = CliRunner()
TABLE_HEADER = (
    "| engine | chunks | mean chunk chars | hit@3 | mean best score | embed tokens"
    " | cost (USD) | elapsed (s) |"
)


def fixture_page(url: str = AVTALA) -> Page:
    return parse_page(FixtureFetcher(FIXTURES).fetch_page(url))


class FakeVoyageClient:
    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._errors = list(errors or [])

    def embed(self, texts: list[str], model: str, input_type: str, **_: object) -> SimpleNamespace:
        assert model == "voyage-3.5-lite"
        self.calls.append((list(texts), input_type))
        if self._errors:
            raise self._errors.pop(0)
        return SimpleNamespace(embeddings=[[1.0, float(len(t))] for t in texts])


def llama_voyage(
    client: FakeVoyageClient, sleeps: list[float], max_batch_tokens: int = 8000
) -> LlamaVoyageEmbedder:
    embedding = VoyageEmbedding(model_name="voyage-3.5-lite", voyage_api_key="test-key")
    embedding._client = client
    return LlamaVoyageEmbedder(
        "test-key", embedding=embedding, max_batch_tokens=max_batch_tokens, sleep=sleeps.append
    )


def test_criterion_1_llama_chunker_schema_and_sentence_size(settings: Settings) -> None:
    """criterion 1: "`LlamaChunker` with the markdown engine converts a fixture page into chunks carrying url, title, section path, licence and dates identical in schema to the section chunker's, and the sentence engine produces chunks within `chunk_size` characters." """
    page = fixture_page()
    baseline = chunk_page(page, settings.chunk.max_chars, settings.chunk.min_chars)
    markdown = LlamaChunker("llama-markdown", 700, 0).chunk(page)
    assert markdown and [f.name for f in fields(markdown[0])] == [f.name for f in fields(Chunk)]

    def meta(c: Chunk) -> tuple[str, str, str, str, str | None]:
        return (c.url, c.title, c.license, c.fetched_at, c.last_updated)

    assert {meta(c) for c in markdown} == {meta(c) for c in baseline}
    assert meta(markdown[0]) == (AVTALA, "דמי אבטלה", "CC-BY-SA-3.0", "2026-10-13", "2026-08-12")
    sections = {c.section for c in markdown}
    assert "דמי אבטלה > הרשמה בשירות התעסוקה > מה צריך להביא לרישום בלשכה" in sections
    assert all(c.section.split(PATH_SEP)[0] == c.title and c.text for c in markdown)
    assert not any(c.text.startswith("#") for c in markdown)
    ids = [c.id for c in markdown]
    assert len(set(ids)) == len(ids) and ids[0].split("-")[0] == baseline[0].id.split("-")[0]
    assert LlamaChunker("llama-markdown", 700, 0).chunk(page) == markdown

    sentence = LlamaChunker("llama-sentence", 200, 20).chunk(page)
    assert len(sentence) > 1
    assert all(len(c.text) <= 200 and c.section == c.title for c in sentence)
    assert {meta(c) for c in sentence} == {meta(c) for c in baseline}


def test_make_chunker_defaults_to_the_section_chunker(settings: Settings) -> None:
    page = fixture_page()
    assert settings.chunk.engine == "section"
    expected = chunk_page(page, settings.chunk.max_chars, settings.chunk.min_chars)
    assert make_chunker(settings.chunk)(page) == expected
    assert make_chunker(settings.chunk, "llama-sentence")(page) != expected
    assert "engine" not in settings.chunk.model_dump()


def test_criterion_2_llama_voyage_embedder(priced: Prices) -> None:
    """criterion 2: "`LlamaVoyageEmbedder` satisfies the `Embedder` protocol over a stubbed transport, batches like the httpx embedder, and records cost (or marks it estimated)." """
    client, sleeps = FakeVoyageClient(), []
    embedder: Embedder = llama_voyage(client, sleeps, max_batch_tokens=3000)
    assert embedder.model == "voyage-3.5-lite"
    texts = ["א" * (1000 + i) for i in range(20)]
    result = embedder.embed(texts, "document")
    httpx_batches = VoyageEmbedder("k", max_batch_tokens=3000)._batches(texts)
    assert [batch for batch, _ in client.calls] == httpx_batches
    assert [len(b) for b in httpx_batches] == [5, 5, 5, 5]
    assert {kind for _, kind in client.calls} == {"document"}
    assert result.vectors == [[1.0, float(len(t))] for t in texts]
    assert result.usage.estimated and result.usage.input_tokens == sum(map(estimate_tokens, texts))

    guard = make_guard(priced)
    guard.embed(llama_voyage(FakeVoyageClient(), []), ["מי זכאי לדמי אבטלה?"], "query")
    guard.embed(FakeEmbedder(), ["מי זכאי לדמי אבטלה?"], "query")
    llama_call, fake_call = guard.recorder.record.calls
    assert llama_call.cost_estimated and llama_call.cost_usd > 0
    assert not fake_call.cost_estimated


def test_llama_voyage_retries_rate_limits_and_wraps_errors() -> None:
    client, sleeps = FakeVoyageClient([RateLimitError("slow down")]), []
    llama_voyage(client, sleeps).embed(["א"], "query")
    assert [kind for _, kind in client.calls] == ["query", "query"] and sleeps == [20.0]

    with pytest.raises(EmbedError, match="AuthenticationError"):
        llama_voyage(FakeVoyageClient([AuthenticationError("no")]), []).embed(["א"], "query")
    limited = FakeVoyageClient([RateLimitError("x") for _ in range(5)])
    with pytest.raises(EmbedError, match="5 attempt"):
        llama_voyage(limited, []).embed(["א"], "query")


class PricedFake(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.model = "voyage-3.5-lite"


def test_criterion_3_named_collection_leaves_default_untouched(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """criterion 3: "`zchut ingest --engine llama-markdown --collection cmp` indexes the fixture pages into a separate collection and leaves the default collection untouched." """
    monkeypatch.setattr(cli, "_embedder", lambda ctx, engine=None: PricedFake())
    pages = ["ingest", "--from-fixtures", "tests/fixtures/pages"]
    assert runner.invoke(app, pages).exit_code == 0
    chroma = cli_root / ".chroma"
    before = sorted(c.id + c.section for c in ChromaStore(chroma).chunks())

    result = runner.invoke(app, [*pages, "--engine", "llama-markdown", "--collection", "cmp"])
    assert result.exit_code == 0, result.output
    assert sorted(c.id + c.section for c in ChromaStore(chroma).chunks()) == before
    fetcher = FixtureFetcher(FIXTURES)
    chunker = LlamaChunker("llama-markdown", 700, 70).chunk
    expected = [c for u in fetcher.urls() for c in chunker(parse_page(fetcher.fetch_page(u)))]
    named = ChromaStore(chroma, "cmp")
    assert named.count() == len(expected) != len(before)
    assert {c.section for c in named.chunks()} == {c.section for c in expected}

    result = runner.invoke(app, ["eval", "--retrieval-only", "--collection", "cmp"])
    assert result.exit_code == 0, result.output
    assert "hit@3:" in result.output
    assert ChromaStore(chroma, "cmp").count() == len(expected)


@pytest.mark.parametrize(
    ("candidates", "adopted"),
    [
        ([(0.8, 0.585)], "llama-markdown + httpx"),
        ([(0.8, 0.575)], None),
        ([(0.77, 0.9)], None),
        ([(0.9, 0.7), (0.9, 0.71), (0.95, 0.59)], "section + llama"),
        ([(None, None)], None),
    ],
)
def test_recommend_rule(
    candidates: list[tuple[float | None, float | None]], adopted: str | None
) -> None:
    names = ["llama-markdown + httpx", "llama-sentence + httpx", "section + llama"]

    def row(engine: str, hit: float | None, best: float | None) -> CompareRow:
        return CompareRow(
            engine=engine,
            chunks=1,
            mean_chunk_chars=1.0,
            hit_at_3=hit,
            mean_best_score=best,
            embed_tokens=1,
            cost_usd=0.0,
            cost_estimated=False,
            elapsed_s=0.0,
        )

    rows = [
        row("section + httpx", 0.8, 0.6),
        *(row(n, *c) for n, c in zip(names, candidates, strict=False)),
    ]
    line = recommend(rows)
    if adopted is None:
        assert line.startswith("המלצה: להשאיר את section + httpx")
    else:
        assert line.startswith(f"המלצה: לאמץ את {adopted}:")


def test_criterion_4_compare_dry_run(cli_root: Path) -> None:
    """criterion 4: "`zchut compare --dry-run` prints the four-row table with the documented columns, writes the report, and prints the recommendation line computed by the stated rule." """
    config = (cli_root / "config" / "zchut.json").read_text("utf-8")
    result = runner.invoke(app, ["compare", "--dry-run"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = lines.index(TABLE_HEADER)
    rows = [line.split(" | ")[0].lstrip("| ") for line in lines[start + 2 : start + 6]]
    assert rows == [
        "section + httpx",
        "llama-markdown + httpx",
        "llama-sentence + httpx",
        "section + llama",
    ]
    path = cli_root / "reports" / f"compare-{date.today().isoformat()}.json"
    report = CompareReport.model_validate(json.loads(path.read_text("utf-8")))
    assert report.dry_run and report.pages == 3 and len(report.rows) == 4
    assert all(r.chunks > 0 and r.hit_at_3 is not None for r in report.rows)
    assert report.recommendation == recommend(report.rows)
    assert lines[start + 6] == report.recommendation
    assert (cli_root / "config" / "zchut.json").read_text("utf-8") == config
    assert not (cli_root / ".chroma").exists()
