from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from typing import Protocol

import anthropic
import httpx
from pydantic import ValidationError

from zchut.answer.model import AnswerError, haiku_json
from zchut.answer.prompt import numbered_sources
from zchut.config import JevSettings
from zchut.graph.state import Verdict
from zchut.http_retry import send_with_backoff
from zchut.run.cost_guard import CostGuard
from zchut.tokens import estimate_tokens
from zchut.types import Hit, Usage

FAKE_VERIFIER_MODEL = "fake-verifier"
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
VERIFY_SYSTEM = (
    "אתה בודק עובדות. קיבלת קטעי מקור ממוספרים וטיוטת תשובה. "
    "בדוק אם כל טענה בטיוטה נתמכת בקטעים. "
    "טענה אינה נתמכת רק אם אין קטע שאומר אותה, או אם מספר, סכום, תאריך או פרק זמן בה "
    "שונה מהכתוב בקטע. ניסוח במילים אחרות, השמטת פרטים ואמירה זהירה כמו 'זה תלוי בנסיבות' "
    "אינם הופכים טענה ללא נתמכת. החזר JSON בלבד לפי הסכמה: "
    "supported הוא true רק כשכל הטענות נתמכות; unsupported_claims מפרט כל טענה שאינה נתמכת "
    "במשפט קצר בעברית."
)
VERIFY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["supported", "unsupported_claims"],
    "additionalProperties": False,
}
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CITED = re.compile(r"\[\d+(?:,\s*\d+)*\]")


class Verifier(Protocol):
    tier: str
    models: tuple[str, ...]
    max_tokens: int

    def verify(self, draft: str, hits: Sequence[Hit], guard: CostGuard) -> Verdict: ...


def verification_input(draft: str, hits: Sequence[Hit]) -> str:
    return f"{numbered_sources(hits)}\n\nטיוטה:\n{draft}"


class HaikuVerifier:
    def __init__(self, client: anthropic.Anthropic, model: str, max_tokens: int) -> None:
        self.tier = "haiku"
        self.models: tuple[str, ...] = (model,)
        self.max_tokens = max_tokens
        self._client = client

    def verify(self, draft: str, hits: Sequence[Hit], guard: CostGuard) -> Verdict:
        model, user = self.models[0], verification_input(draft, hits)

        def send() -> tuple[Verdict, Usage]:
            data, usage = haiku_json(
                self._client, model, self.max_tokens, VERIFY_SYSTEM, user, VERIFY_SCHEMA
            )
            try:
                return Verdict.model_validate(data), usage
            except ValidationError as exc:
                raise AnswerError(f"{model} returned an invalid verdict") from exc

        return guard.call("verify", model, VERIFY_SYSTEM + user, self.max_tokens, send)


def jev_request(model: str, draft: str, hits: Sequence[Hit], jev: JevSettings) -> dict[str, object]:
    return {
        "model": model,
        "state": {"draft": draft, "sources": numbered_sources(hits)},
        "questions": {
            "decision": {
                "type": "choice",
                "instructions": jev.instructions,
                "criteria": jev.criteria.model_dump(),
            }
        },
    }


class JevVerifier:
    def __init__(
        self,
        client: httpx.Client,
        api_key: str,
        model: str,
        jev: JevSettings,
        claims: HaikuVerifier,
        *,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tier = "jev"
        self.models: tuple[str, ...] = (model, *claims.models)
        self.max_tokens = claims.max_tokens
        self._client = client
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._jev = jev
        self._claims = claims
        self._max_attempts = max_attempts
        self._sleep = sleep

    def verify(self, draft: str, hits: Sequence[Hit], guard: CostGuard) -> Verdict:
        model = self.models[0]
        body = jev_request(model, draft, hits, self._jev)
        choice = guard.call(
            "verify", model, verification_input(draft, hits), 0, lambda: self._decide(body)
        )
        if choice == "supported":
            return Verdict(supported=True)
        listed = self._claims.verify(draft, hits, guard)
        return Verdict(supported=False, unsupported_claims=listed.unsupported_claims)

    def _decide(self, body: dict[str, object]) -> tuple[str, Usage]:
        response, attempts = send_with_backoff(
            lambda: self._post(body), self._max_attempts, self._sleep
        )
        if response.status_code != 200:
            raise AnswerError(f"jev answered {response.status_code} after {attempts} attempt(s)")
        try:
            payload = response.json()
            choice = str(payload["answers"]["decision"]["choice"])
            u = payload["usage"]
            usage = Usage(int(u["input_tokens"]), int(u.get("output_tokens", 0)))
        except (ValueError, KeyError, TypeError) as exc:
            raise AnswerError(f"unexpected jev response: {exc}") from exc
        if choice not in ("supported", "unsupported"):
            raise AnswerError(f"jev returned an unknown choice {choice!r}")
        return choice, usage

    def _post(self, body: dict[str, object]) -> httpx.Response:
        try:
            return self._client.post(OPENROUTER_DECISIONS_URL, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise AnswerError(f"jev request failed: {type(exc).__name__}") from exc


class FakeVerifier:
    def __init__(self, tier: str = "haiku", max_tokens: int = 400) -> None:
        self.tier = tier
        self.models: tuple[str, ...] = (FAKE_VERIFIER_MODEL,)
        self.max_tokens = max_tokens

    def verify(self, draft: str, hits: Sequence[Hit], guard: CostGuard) -> Verdict:
        text = verification_input(draft, hits)

        def send() -> tuple[Verdict, Usage]:
            claims = uncited_sentences(draft)
            verdict = Verdict(supported=not claims, unsupported_claims=claims)
            return verdict, Usage(estimate_tokens(text), estimate_tokens(verdict.model_dump_json()))

        return guard.call("verify", self.models[0], text, self.max_tokens, send)


def uncited_sentences(draft: str) -> list[str]:
    sentences = (s.strip() for s in _SENTENCE_END.split(draft))
    return [s.rstrip(".!?") for s in sentences if s and not _CITED.search(s)]
