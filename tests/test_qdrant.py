from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from typer.testing import CliRunner

from conftest import AVTALA, FIXTURES, make_guard
from zchut import cli
from zchut.config import Prices, Settings
from zchut.index.embedder import FakeEmbedder
from zchut.index.ingest import ingest
from zchut.index.store import (
    ChromaStore,
    InMemoryVectorStore,
    QdrantStore,
    StoreError,
    VectorStore,
    migrate,
)
from zchut.sources.fetch import FixtureFetcher

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")
runner = CliRunner()
DIMS = 512


def _fill(store: VectorStore, settings: Settings, priced: Prices, dims: int = DIMS) -> int:
    fetcher = FixtureFetcher(FIXTURES)
    chunk = settings.chunk
    embedder = FakeEmbedder(dims)
    guard = make_guard(priced)
    return ingest(
        fetcher.urls(), fetcher, embedder, store, guard, chunk.max_chars, chunk.min_chars
    ).chunks


def test_criterion_4_qdrant_store_matches_the_in_memory_store_and_migrate_checks_counts(
    settings: Settings, priced: Prices, tmp_path: Path
) -> None:
    """criterion 4: "`QdrantStore.replace`, `search`, `delete`, `count` behave like `InMemoryVectorStore` on a stubbed Qdrant client (re-ingest replaces, search returns scores and metadata); `store migrate` copies all chunks and fails loudly if the counts differ." """
    qdrant, memory = QdrantStore(QdrantClient(":memory:"), DIMS), InMemoryVectorStore()
    total = _fill(qdrant, settings, priced)
    assert _fill(memory, settings, priced) == total and qdrant.count() == memory.count() == total
    assert _fill(qdrant, settings, priced) == total and qdrant.count() == total

    [vector] = FakeEmbedder(DIMS).embed(["מי זכאי לדמי אבטלה"], "query").vectors
    got, want = qdrant.search(vector, 5), memory.search(vector, 5)
    assert [h.chunk for h in got] == [h.chunk for h in want]
    assert [h.score for h in got] == pytest.approx([h.score for h in want])
    assert got[0].chunk.url == AVTALA and got[0].chunk.license == "CC-BY-SA-3.0"

    qdrant.delete(AVTALA)
    memory.delete(AVTALA)
    assert qdrant.count() == memory.count() < total
    assert AVTALA not in {h.chunk.url for h in qdrant.search(vector, 20)}

    chroma = ChromaStore(tmp_path / "chroma")
    _fill(chroma, settings, priced)
    target = QdrantStore(QdrantClient(":memory:"), DIMS)
    assert migrate(chroma, target, batch=7) == chroma.count() == total
    assert [h.chunk for h in target.search(vector, 3)] == [
        h.chunk for h in chroma.search(vector, 3)
    ]

    crowded = QdrantStore(QdrantClient(":memory:"), DIMS)
    stray = replace(target.search(vector, 1)[0].chunk, id="stray", url="https://example.org/x")
    crowded.replace(stray.url, [stray], [vector])
    with pytest.raises(
        StoreError, match=f"source holds {total} chunks but the target holds {total + 1}"
    ):
        migrate(chroma, crowded)


def test_store_migrate_command_copies_chroma_into_qdrant(
    cli_root: Path, settings: Settings, priced: Prices, monkeypatch: pytest.MonkeyPatch
) -> None:
    total = _fill(ChromaStore(cli_root / ".chroma"), settings, priced, 1024)
    client = QdrantClient(":memory:")
    monkeypatch.setattr(cli, "QdrantClient", lambda **_kw: client)
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.invalid")
    monkeypatch.setenv("QDRANT_API_KEY", "test-key")
    result = runner.invoke(cli.app, ["store", "migrate", "--from", "chroma", "--to", "qdrant"])
    assert result.exit_code == 0, result.output
    assert f"הועתקו {total} מקטעים" in result.output
    assert QdrantStore(client).count() == total

    monkeypatch.delenv("QDRANT_API_KEY")
    result = runner.invoke(cli.app, ["store", "migrate"])
    assert result.exit_code == 1 and "QDRANT_API_KEY" in result.output


def test_qdrant_port_defaults_to_443_for_https_without_a_port() -> None:
    from zchut.cli import qdrant_port

    assert qdrant_port("https://x.cloud.qdrant.io") == 443
    assert qdrant_port("https://x.cloud.qdrant.io:6333") == 6333
    assert qdrant_port("http://localhost") == 6333
    assert qdrant_port("https://x.cloud.qdrant.io:8443/") == 8443
