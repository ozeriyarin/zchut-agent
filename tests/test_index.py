from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import AVTALA, FIXTURES, TAX, make_guard
from zchut.answer.model import FakeAnswerModel
from zchut.config import Prices, Settings
from zchut.index.embedder import EmbedError, FakeEmbedder, VoyageEmbedder
from zchut.index.ingest import embed_text, ingest
from zchut.index.store import ChromaStore, InMemoryVectorStore, cosine
from zchut.run.ask import AskDeps, retrieve
from zchut.sources.fetch import FixtureFetcher
from zchut.types import Chunk


def voyage(requests: list[dict[str, object]], statuses: list[int] | None = None) -> VoyageEmbedder:
    queue = list(statuses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.voyageai.com/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        requests.append(body)
        if queue:
            return httpx.Response(queue.pop(0))
        data = [{"index": i, "embedding": [1.0, float(i)]} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 7}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return VoyageEmbedder("test-key", client=client)


def test_criterion_4_interfaces_and_fakes(settings: Settings, priced: Prices) -> None:
    """criterion 4: "The embedder and the store sit behind interfaces with offline fakes: `FakeEmbedder` is deterministic (same text, same vector), `InMemoryVectorStore` returns top-k by cosine with scores and metadata, ingesting the same URL twice replaces its chunks instead of duplicating them, and `VoyageEmbedder` builds its request with model `voyage-3.5-lite` and `input_type` `document` for ingest and `query` for ask against a stubbed transport." """
    fake = FakeEmbedder()
    assert fake.vector("דמי אבטלה") == FakeEmbedder().vector("דמי אבטלה")
    assert fake.vector("דמי אבטלה") != fake.vector("החזר מס")

    store = InMemoryVectorStore()
    fetcher = FixtureFetcher(FIXTURES)
    chunk = settings.chunk
    for _ in range(2):
        ingest(fetcher.urls(), fetcher, fake, store, make_guard(priced), **chunk.model_dump())
    once = InMemoryVectorStore()
    ingest(fetcher.urls(), fetcher, fake, once, make_guard(priced), **chunk.model_dump())
    assert store.count() == once.count() > 0

    query = fake.vector("איך מגישים בקשה להחזר מס")
    hits = store.search(query, 3)
    assert len(hits) == 3
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    assert hits[0].chunk.url == TAX and hits[0].chunk.license == "gov.il"
    assert hits[0].score == pytest.approx(cosine(query, fake.vector(embed_text(hits[0].chunk))))

    requests: list[dict[str, object]] = []
    embedder = voyage(requests)
    voyage_store = InMemoryVectorStore()
    ingest([AVTALA], fetcher, embedder, voyage_store, make_guard(priced), **chunk.model_dump())
    assert retrieve(
        "מי זכאי?", AskDeps(embedder, voyage_store, FakeAnswerModel(), 5, 0.3), make_guard(priced)
    )
    assert [(r["model"], r["input_type"]) for r in requests] == [
        ("voyage-3.5-lite", "document"),
        ("voyage-3.5-lite", "query"),
    ]
    assert requests[1]["input"] == ["מי זכאי?"]


def test_voyage_retries_once_then_fails() -> None:
    requests: list[dict[str, object]] = []
    assert voyage(requests, [503]).embed(["א"], "query").usage.input_tokens == 7
    with pytest.raises(EmbedError, match="429"):
        voyage(requests, [429, 429]).embed(["א"], "query")
    assert len(requests) == 4


def test_chroma_store_replaces_and_searches(tmp_path: Path) -> None:
    store = ChromaStore(tmp_path / "chroma")
    base = Chunk(
        "a-000", AVTALA, "דמי אבטלה", "דמי אבטלה", "טקסט", "2026-10-13", "CC-BY-SA-3.0", None
    )
    other = Chunk(
        "a-001",
        AVTALA,
        "דמי אבטלה",
        "דמי אבטלה > א",
        "עוד",
        "2026-10-13",
        "CC-BY-SA-3.0",
        "2026-08-12",
    )
    store.replace(AVTALA, [base, other], [[1.0, 0.0], [0.6, 0.8]])
    store.replace(AVTALA, [base, other], [[1.0, 0.0], [0.6, 0.8]])
    assert store.count() == 2
    hits = store.search([1.0, 0.0], 2)
    assert [h.chunk for h in hits] == [base, other]
    assert [h.score for h in hits] == [pytest.approx(1.0), pytest.approx(0.6, abs=1e-6)]
    assert ChromaStore(tmp_path / "chroma").count() == 2
