from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import anthropic
from pydantic import BaseModel, Field, ValidationError

from zchut.answer.model import AnswerError, haiku_json
from zchut.graph.classify import fake_topic
from zchut.run.cost_guard import CostGuard
from zchut.tokens import estimate_tokens
from zchut.types import Usage

FAKE_CONDENSER_MODEL = "fake-condenser"
NUDGE_LINE = "לא בטוח שהבנתי שאלה. על מה לבדוק עבורך? למשל: דמי אבטלה, פנסיה, החזר מס."
CONDENSE_SYSTEM = (
    "אתה מנסח מחדש הודעות בשיחה על זכויות ובירוקרטיה בישראל. החזר JSON בלבד לפי הסכמה. "
    "קיבלת את השאלות הקודמות בשיחה, אולי שאלה שממתינה לתשובת הבהרה, ואת ההודעה האחרונה. "
    "standalone_question: שאלה אחת בעברית שאפשר להבין בלי השיחה. פתור כינויים והשמטות לפי "
    "השאלות הקודמות (למשל 'המשרד שלהם' אחרי שאלה על דמי אבטלה הוא המשרד של שירות התעסוקה). "
    "אם ההודעה עונה על שאלת ההבהרה, שלב את התשובה בשאלה הממתינה לשאלה אחת. "
    "אם ההודעה כבר עומדת בפני עצמה, החזר אותה בדיוק כפי שהיא. "
    "is_reply_to_clarification: true רק כשיש שאלה ממתינה וההודעה עונה עליה. "
    "is_question: false כשההודעה אינה שאלה ואינה בקשת מידע, למשל תגובה כמו 'מה קשור?'; "
    "אחרת true."
)
CONDENSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "standalone_question": {"type": "string"},
        "is_reply_to_clarification": {"type": "boolean"},
        "is_question": {"type": "boolean"},
    },
    "required": ["standalone_question", "is_reply_to_clarification", "is_question"],
    "additionalProperties": False,
}
_QUESTION_WORD = re.compile(r"(?<!\w)ו?(?:מי|מה|איך|איפה|היכן|כמה|מתי|למה|האם|אילו)(?!\w)")
_REACTION = re.compile(r"^(?:מה קשור|[^\s?]+)\?$")
_PRONOUN = re.compile(r"(\S+) (?:שלהם|שלו|שלה)(?!\w)")
_NAMES = {"unemployment": "שירות התעסוקה", "pension": "קרן הפנסיה", "tax": "רשות המסים"}


class Condensed(BaseModel):
    standalone_question: str = Field(min_length=1)
    is_reply_to_clarification: bool
    is_question: bool


@dataclass(frozen=True)
class Turns:
    history: Sequence[str]
    message: str
    pending: str | None = None
    asked: str | None = None

    def prompt(self) -> str:
        lines = ["שאלות קודמות:", *(f"{n}. {q}" for n, q in enumerate(self.history, start=1))]
        if self.pending:
            lines += [f"שאלה ממתינה להבהרה: {self.pending}", f"שאלת ההבהרה: {self.asked or ''}"]
        return "\n".join([*lines, f"הודעה אחרונה: {self.message}"])


class Condenser(Protocol):
    model: str
    max_tokens: int

    def condense(self, turns: Turns, guard: CostGuard) -> Condensed: ...


class HaikuCondenser:
    def __init__(self, client: anthropic.Anthropic, model: str, max_tokens: int) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    def condense(self, turns: Turns, guard: CostGuard) -> Condensed:
        user = turns.prompt()

        def send() -> tuple[Condensed, Usage]:
            data, usage = haiku_json(
                self._client, self.model, self.max_tokens, CONDENSE_SYSTEM, user, CONDENSE_SCHEMA
            )
            try:
                return Condensed.model_validate(data), usage
            except ValidationError as exc:
                raise AnswerError(f"{self.model} returned an invalid condensed question") from exc

        return guard.call("condense", self.model, CONDENSE_SYSTEM + user, self.max_tokens, send)


class FakeCondenser:
    def __init__(self, max_tokens: int = 200) -> None:
        self.model = FAKE_CONDENSER_MODEL
        self.max_tokens = max_tokens

    def condense(self, turns: Turns, guard: CostGuard) -> Condensed:
        def send() -> tuple[Condensed, Usage]:
            result = fake_condense(turns)
            usage = Usage(
                estimate_tokens(turns.prompt()), estimate_tokens(result.model_dump_json())
            )
            return result, usage

        return guard.call("condense", self.model, turns.prompt(), self.max_tokens, send)


def _condensed(text: str, reply: bool = False, question: bool = True) -> Condensed:
    return Condensed(
        standalone_question=text, is_reply_to_clarification=reply, is_question=question
    )


def fake_condense(turns: Turns) -> Condensed:
    text = turns.message.strip()
    if _REACTION.match(text):
        return _condensed(text, question=False)
    asks = text.endswith("?") or _QUESTION_WORD.search(text)
    last = turns.history[-1] if turns.history else None
    if not asks and (turns.pending or last):
        return _condensed(f"{turns.pending or last} ({text})", reply=bool(turns.pending))
    name = _NAMES.get(fake_topic(last)) if last else None
    return _condensed(_PRONOUN.sub(rf"\1 של {name}", text) if name else text)
