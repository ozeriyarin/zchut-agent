from __future__ import annotations

import re
from typing import Protocol

import anthropic

from zchut.answer.prompt import Prompt
from zchut.tokens import estimate_tokens
from zchut.types import ModelReply, Usage

FAKE_ANSWER_MODEL = "fake-answer-model"


class AnswerError(Exception):
    pass


class AnswerModel(Protocol):
    model: str
    max_tokens: int

    def answer(self, prompt: Prompt) -> ModelReply: ...


class HaikuAnswerModel:
    def __init__(
        self, client: anthropic.Anthropic, model: str = "claude-haiku-4-5", max_tokens: int = 800
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    def answer(self, prompt: Prompt) -> ModelReply:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=prompt.system,
                messages=[{"role": "user", "content": prompt.user}],
            )
        except anthropic.APIError as exc:
            raise AnswerError(f"answer model call failed: {type(exc).__name__}") from exc
        text = "".join(block.text for block in message.content if block.type == "text")
        u = message.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        return ModelReply(text=text, usage=usage)


def make_haiku_client(api_key: str, timeout_s: float) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=1)


class FakeAnswerModel:
    def __init__(self, canned: str | None = None, max_tokens: int = 800) -> None:
        self.model = FAKE_ANSWER_MODEL
        self.max_tokens = max_tokens
        self.calls = 0
        self._canned = canned

    def answer(self, prompt: Prompt) -> ModelReply:
        self.calls += 1
        text = self._canned if self._canned is not None else self._compose(prompt)
        usage = Usage(estimate_tokens(prompt.system + prompt.user), estimate_tokens(text))
        return ModelReply(text=text, usage=usage)

    @staticmethod
    def _compose(prompt: Prompt) -> str:
        sentences = []
        for n, hit in enumerate(prompt.hits[:2], start=1):
            first = re.split(r"(?<=[.!?])\s", hit.chunk.text.replace("\n", " "), maxsplit=1)[0]
            sentences.append(f"{first.rstrip('.')} [{n}].")
        return " ".join(sentences)
