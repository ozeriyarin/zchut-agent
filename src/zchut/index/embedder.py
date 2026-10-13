from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Sequence
from typing import Literal, Protocol

import httpx

from zchut.http_retry import send_with_backoff
from zchut.tokens import estimate_tokens
from zchut.types import EmbedResult, Usage

InputType = Literal["document", "query"]
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
FAKE_EMBED_MODEL = "fake-embedder"
MAX_BATCH_TEXTS = 128


class EmbedError(Exception):
    pass


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbedResult: ...


class VoyageEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3.5-lite",
        client: httpx.Client | None = None,
        *,
        max_retries: int = 5,
        max_batch_tokens: int = 8000,
        min_interval_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30.0)
        self._max_retries = max(1, max_retries)
        self._max_batch_tokens = max_batch_tokens
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbedResult:
        vectors: list[list[float]] = []
        tokens = 0
        for batch in self._batches(texts):
            batch_vectors, batch_tokens = self._embed_batch(batch, input_type)
            vectors.extend(batch_vectors)
            tokens += batch_tokens
        return EmbedResult(vectors=vectors, usage=Usage(input_tokens=tokens))

    def _batches(self, texts: Sequence[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        batch: list[str] = []
        batch_tokens = 0
        for text in texts:
            cost = estimate_tokens(text)
            full = len(batch) >= MAX_BATCH_TEXTS or batch_tokens + cost > self._max_batch_tokens
            if batch and full:
                batches.append(batch)
                batch, batch_tokens = [], 0
            batch.append(text)
            batch_tokens += cost
        return [*batches, batch] if batch else batches

    def _embed_batch(
        self, texts: list[str], input_type: InputType
    ) -> tuple[list[list[float]], int]:
        body: dict[str, object] = {"input": texts, "model": self.model, "input_type": input_type}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        response, attempts = send_with_backoff(
            lambda: self._post(body, headers), self._max_retries, self._sleep
        )
        if response.status_code != 200:
            raise EmbedError(f"voyage answered {response.status_code} after {attempts} attempt(s)")
        try:
            payload = response.json()
            rows = sorted(payload["data"], key=lambda row: int(row["index"]))
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
            tokens = int(payload["usage"]["total_tokens"])
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbedError(f"unexpected voyage response: {exc}") from exc
        if len(vectors) != len(texts):
            raise EmbedError("voyage returned a different number of vectors")
        return vectors, tokens

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval_s - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _post(self, body: dict[str, object], headers: dict[str, str]) -> httpx.Response:
        self._pace()
        try:
            return self._client.post(VOYAGE_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbedError(f"voyage request failed: {type(exc).__name__}") from exc


class FakeEmbedder:
    def __init__(self, dim: int = 512) -> None:
        self.model = FAKE_EMBED_MODEL
        self._dim = dim

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbedResult:
        tokens = sum(estimate_tokens(t) for t in texts)
        return EmbedResult(vectors=[self.vector(t) for t in texts], usage=Usage(tokens))

    def vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for word in re.findall(r"\w+", text.lower()):
            padded = f" {word} "
            for i in range(len(padded) - 2):
                digest = hashlib.blake2b(padded[i : i + 3].encode("utf-8"), digest_size=4)
                vec[int.from_bytes(digest.digest(), "big") % self._dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]
