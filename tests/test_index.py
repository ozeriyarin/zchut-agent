from __future__ import annotations

import json
from pathlib import Path
from typing import cast

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


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def voyage(
    requests: list[dict[str, object]],
    statuses: list[httpx.Response] | None = None,
    time: FakeTime | None = None,
    max_batch_tokens: int = 8000,
    min_interval_s: float = 0.0,
) -> VoyageEmbedder:
    queue = list(statuses or [])
    fake = time or FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.voyageai.com/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        requests.append(body)
        if queue:
            return queue.pop(0)
        rows = enumerate(body["input"])
        data = [{"index": i, "embedding": [1.0, float(len(t))]} for i, t in rows][::-1]
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 7}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return VoyageEmbedder(
        "test-key",
        client=client,
        max_batch_tokens=max_batch_tokens,
        min_interval_s=min_interval_s,
        clock=fake.clock,
        sleep=fake.sleep,
    )


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


def test_voyage_honours_retry_after_on_429() -> None:
    requests: list[dict[str, object]] = []
    fake = FakeTime()
    limited = httpx.Response(429, headers={"Retry-After": "7"})
    result = voyage(requests, [limited], fake).embed(["א"], "query")
    assert result.usage.input_tokens == 7 and len(requests) == 2
    assert fake.sleeps == [7.0]


def test_voyage_backs_off_exponentially_without_retry_after() -> None:
    requests: list[dict[str, object]] = []
    fake = FakeTime()
    statuses = [httpx.Response(429), httpx.Response(503)]
    assert voyage(requests, statuses, fake).embed(["א"], "query").usage.input_tokens == 7
    assert fake.sleeps == [20.0, 40.0]


def test_voyage_raises_after_max_retries() -> None:
    requests: list[dict[str, object]] = []
    fake = FakeTime()
    with pytest.raises(EmbedError, match="429 after 5 attempt"):
        voyage(requests, [httpx.Response(429) for _ in range(5)], fake).embed(["א"], "query")
    assert len(requests) == 5
    assert fake.sleeps == [20.0, 40.0, 60.0, 60.0]


def test_voyage_does_not_retry_client_errors() -> None:
    requests: list[dict[str, object]] = []
    with pytest.raises(EmbedError, match="401"):
        voyage(requests, [httpx.Response(401)]).embed(["א"], "query")
    assert len(requests) == 1


def test_voyage_batches_by_token_cap_in_order() -> None:
    requests: list[dict[str, object]] = []
    fake = FakeTime()
    texts = ["א" * (1000 + i) for i in range(20)]
    statuses = [httpx.Response(429)]
    result = voyage(requests, statuses, fake, max_batch_tokens=3000).embed(texts, "document")
    inputs = [cast(list[str], r["input"]) for r in requests]
    assert [len(batch) for batch in inputs] == [5, 5, 5, 5, 5]
    assert inputs[0] == inputs[1]
    assert [t for batch in inputs[1:] for t in batch] == texts
    assert fake.sleeps == [20.0]
    assert result.vectors == [[1.0, float(len(t))] for t in texts]
    assert result.usage.input_tokens == 4 * 7


def test_voyage_paces_requests_by_min_interval() -> None:
    requests: list[dict[str, object]] = []
    fake = FakeTime()
    texts = ["א" * 1000 for _ in range(3)]
    embedder = voyage(requests, None, fake, max_batch_tokens=600, min_interval_s=20)
    embedder.embed(texts, "document")
    assert len(requests) == 3
    assert fake.sleeps == [20.0, 20.0]


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
