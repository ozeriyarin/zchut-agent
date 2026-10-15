from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import replace

from llama_index.embeddings.voyageai import VoyageEmbedding
from voyageai.error import RateLimitError, ServerError, ServiceUnavailableError, VoyageError

from zchut.http_retry import capped_backoff_s
from zchut.index.embedder import MAX_BATCH_TEXTS, EmbedError, InputType, VoyageEmbedder
from zchut.tokens import estimate_tokens
from zchut.types import EmbedResult

_RETRYABLE = (RateLimitError, ServerError, ServiceUnavailableError)


class LlamaVoyageEmbedder(VoyageEmbedder):
    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3.5-lite",
        *,
        embedding: VoyageEmbedding | None = None,
        max_retries: int = 5,
        max_batch_tokens: int = 8000,
        min_interval_s: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            api_key,
            model,
            max_retries=max_retries,
            max_batch_tokens=max_batch_tokens,
            min_interval_s=min_interval_s,
            sleep=sleep,
        )
        self._embedding = embedding or VoyageEmbedding(
            model_name=model, voyage_api_key=api_key, embed_batch_size=MAX_BATCH_TEXTS
        )

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbedResult:
        # The LlamaIndex wrapper drops Voyage's usage block, so tokens are estimated from chars.
        result = super().embed(texts, input_type)
        return replace(result, usage=replace(result.usage, estimated=True))

    def _embed_batch(
        self, texts: list[str], input_type: InputType
    ) -> tuple[list[list[float]], int]:
        for attempt in range(self._max_retries):
            self._pace()
            try:
                # _embed is the wrapper's one request primitive that takes an input type.
                vectors = self._embedding._embed(texts, input_type)
                break
            except _RETRYABLE as exc:
                if attempt + 1 >= self._max_retries:
                    raise EmbedError(
                        f"voyage (llama) failed after {attempt + 1} attempt(s)"
                    ) from exc
                self._sleep(capped_backoff_s(attempt))
            except VoyageError as exc:
                raise EmbedError(f"voyage (llama) request failed: {type(exc).__name__}") from exc
        if len(vectors) != len(texts):
            raise EmbedError("voyage (llama) returned a different number of vectors")
        return [[float(x) for x in v] for v in vectors], sum(estimate_tokens(t) for t in texts)
