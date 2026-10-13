from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Literal, Protocol

import httpx

from zchut.tokens import estimate_tokens
from zchut.types import EmbedResult, Usage

InputType = Literal["document", "query"]
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
FAKE_EMBED_MODEL = "fake-embedder"


class EmbedError(Exception):
    pass


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbedResult: ...


class VoyageEmbedder:
    def __init__(
        self, api_key: str, model: str = "voyage-3.5-lite", client: httpx.Client | None = None
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30.0)

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbedResult:
        body: dict[str, object] = {
            "input": list(texts),
            "model": self.model,
            "input_type": input_type,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        response = self._post(body, headers)
        if response.status_code == 429 or response.status_code >= 500:
            response = self._post(body, headers)
        if response.status_code != 200:
            raise EmbedError(f"voyage answered {response.status_code}")
        try:
            payload = response.json()
            rows = sorted(payload["data"], key=lambda row: int(row["index"]))
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
            tokens = int(payload["usage"]["total_tokens"])
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbedError(f"unexpected voyage response: {exc}") from exc
        if len(vectors) != len(texts):
            raise EmbedError("voyage returned a different number of vectors")
        return EmbedResult(vectors=vectors, usage=Usage(input_tokens=tokens))

    def _post(self, body: dict[str, object], headers: dict[str, str]) -> httpx.Response:
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
