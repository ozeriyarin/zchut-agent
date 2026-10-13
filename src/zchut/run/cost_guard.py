from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from zchut.answer.model import AnswerError, AnswerModel
from zchut.answer.prompt import Prompt
from zchut.config import Prices
from zchut.index.embedder import Embedder, EmbedError, InputType
from zchut.run.records import RunRecorder
from zchut.tokens import estimate_tokens
from zchut.types import Usage

T = TypeVar("T")

BUDGET_MESSAGE = (
    "הריצה נעצרה לפני קריאה שהייתה חוצה את תקרת התקציב: "
    "הוצאו עד כה {spent:.4f}$, הקריאה הבאה מוערכת ב-{estimate:.4f}$, התקרה היא {cap:.4f}$."
)


class BudgetExceeded(Exception):  # noqa: N818
    def __init__(self, spent: float, estimate: float, cap: float) -> None:
        super().__init__(BUDGET_MESSAGE.format(spent=spent, estimate=estimate, cap=cap))


def cost_usd(prices: Prices, model: str, usage: Usage) -> float:
    p = prices.for_model(model)
    return (
        usage.input_tokens * p.input
        + usage.output_tokens * p.output
        + usage.cache_read_tokens * p.cache_read
        + usage.cache_write_tokens * p.cache_write
    ) / 1_000_000


class CostGuard:
    def __init__(self, max_usd: float, prices: Prices, recorder: RunRecorder) -> None:
        self.max_usd = max_usd
        self.prices = prices
        self.recorder = recorder
        self.spent = 0.0

    def embed(
        self, embedder: Embedder, texts: Sequence[str], input_type: InputType
    ) -> list[list[float]]:
        tokens = sum(estimate_tokens(t) for t in texts)
        estimate = cost_usd(self.prices, embedder.model, Usage(tokens))

        def send() -> tuple[list[list[float]], Usage]:
            result = embedder.embed(texts, input_type)
            return result.vectors, result.usage

        return self._call("embed", embedder.model, estimate, send)

    def answer(self, model: AnswerModel, prompt: Prompt) -> str:
        tokens = estimate_tokens(prompt.system + prompt.user)
        estimate = cost_usd(self.prices, model.model, Usage(tokens, model.max_tokens))

        def send() -> tuple[str, Usage]:
            reply = model.answer(prompt)
            return reply.text, reply.usage

        return self._call("answer", model.model, estimate, send)

    def _call(
        self, role: str, model: str, estimate: float, send: Callable[[], tuple[T, Usage]]
    ) -> T:
        if self.spent + estimate > self.max_usd:
            raise BudgetExceeded(self.spent, estimate, self.max_usd)
        started = time.perf_counter()
        result, usage = send()
        latency_ms = int((time.perf_counter() - started) * 1000)
        cost = cost_usd(self.prices, model, usage)
        self.spent += cost
        self.recorder.add_call(role, model, usage, latency_ms, cost)
        return result


@dataclass(frozen=True)
class Outcome:
    output: str
    exit_code: int


def guarded(recorder: RunRecorder, action: Callable[[], str]) -> Outcome:
    try:
        return Outcome(action(), 0)
    except BudgetExceeded as exc:
        recorder.record.budget_exceeded = True
        recorder.record.errors.append(str(exc))
        return Outcome(str(exc), 2)
    except (EmbedError, AnswerError) as exc:
        recorder.record.errors.append(str(exc))
        return Outcome(f"שגיאה: {exc}", 1)
