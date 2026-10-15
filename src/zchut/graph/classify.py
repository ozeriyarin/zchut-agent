from __future__ import annotations

import re
from typing import Protocol, get_args

import anthropic
from pydantic import ValidationError

from zchut.answer.model import AnswerError, haiku_json
from zchut.graph.clarifications import Clarifications
from zchut.graph.state import Classification, Intent, Topic
from zchut.run.cost_guard import CostGuard
from zchut.tokens import estimate_tokens
from zchut.types import Usage

FAKE_CLASSIFIER_MODEL = "fake-classifier"
CLASSIFY_SYSTEM = (
    "אתה מסווג שאלות על זכויות ובירוקרטיה בישראל. החזר JSON בלבד לפי הסכמה. "
    "topic: unemployment לדמי אבטלה ולשירות התעסוקה, pension לפנסיה ולחיסכון, "
    "tax למס הכנסה ולהחזרי מס, other לכל נושא אחר. "
    "in_scope: true רק כשהשאלה עוסקת בזכויות מול ביטוח לאומי, שירות התעסוקה, פנסיה או מס. "
    "office_lookup: true רק כשהשאלה מבקשת איפה נמצא משרד או איך יוצרים איתו קשר. "
    "missing_fact: מפתח אחד מהרשימה שלמטה, רק כשהשאלה היא על סכום או על משך תקופה, "
    "התשובה משתנה לפי העובדה, והנושא מופיע בנושאים של המפתח; אחרת null. "
    "אל תבקש הבהרה לעולם על שאלות זכאות (מי זכאי), איך עושים, איפה, או אילו מסמכים צריך. "
    "intent: greeting לברכה בלבד (היי, שלום), reaction לתגובה שאינה שאלה (תודה, אוקי), "
    "question לכל השאר."
)
_TOPIC_WORDS: list[tuple[Topic, re.Pattern[str]]] = [
    ("unemployment", re.compile(r"אבטלה|תעסוקה|פיטורי|התפטר")),
    ("pension", re.compile(r"פנסי|הפרש")),
    ("tax", re.compile(r"(?<!\w)[הלב]?מס(?!\w)|טופס 106")),
]
_AMOUNT = re.compile(r"כמה.*(?:אקבל|מגיע)|גובה")
_OFFICE = re.compile(r"(?:איפה|היכן).{0,20}(?:משרד|לשכ|סניף)|ליצור קשר|פרטי (?:ה)?קשר")
_GREETING = re.compile(r"(?:היי|הי|שלום|בוקר טוב)[\s!.,]*")
_REACTION = re.compile(r"(?:תודה|אוקיי?)[\s!.,]*")


def classify_system(clarifications: Clarifications) -> str:
    keys = [
        f"{c.key}: {c.question_he} (נושאים: {', '.join(c.applies_to_topics)})"
        for c in clarifications.values()
    ]
    return "\n".join([CLASSIFY_SYSTEM, "מפתחות:", *keys])


def classify_schema(clarifications: Clarifications) -> dict[str, object]:
    fact = {"anyOf": [{"type": "string", "enum": list(clarifications)}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "enum": list(get_args(Topic))},
            "in_scope": {"type": "boolean"},
            "office_lookup": {"type": "boolean"},
            "missing_fact": fact,
            "intent": {"type": "string", "enum": list(get_args(Intent))},
        },
        "required": ["topic", "in_scope", "office_lookup", "missing_fact", "intent"],
        "additionalProperties": False,
    }


class Classifier(Protocol):
    model: str
    max_tokens: int

    def classify(self, text: str, guard: CostGuard) -> Classification: ...


class HaikuClassifier:
    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str,
        max_tokens: int,
        clarifications: Clarifications,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client
        self._system = classify_system(clarifications)
        self._schema = classify_schema(clarifications)

    def classify(self, text: str, guard: CostGuard) -> Classification:
        def send() -> tuple[Classification, Usage]:
            data, usage = haiku_json(
                self._client, self.model, self.max_tokens, self._system, text, self._schema
            )
            try:
                return Classification.model_validate(data), usage
            except ValidationError as exc:
                raise AnswerError(f"{self.model} returned an invalid classification") from exc

        return guard.call("classify", self.model, self._system + text, self.max_tokens, send)


class FakeClassifier:
    def __init__(self, max_tokens: int = 200) -> None:
        self.model = FAKE_CLASSIFIER_MODEL
        self.max_tokens = max_tokens

    def classify(self, text: str, guard: CostGuard) -> Classification:
        def send() -> tuple[Classification, Usage]:
            result = fake_classification(text)
            return result, Usage(estimate_tokens(text), estimate_tokens(result.model_dump_json()))

        return guard.call("classify", self.model, text, self.max_tokens, send)


def fake_topic(text: str) -> Topic:
    return next((t for t, words in _TOPIC_WORDS if words.search(text)), "other")


def asks_office(text: str) -> bool:
    return bool(_OFFICE.search(text))


def fake_intent(text: str) -> Intent:
    t = text.strip()
    if _GREETING.fullmatch(t):
        return "greeting"
    return "reaction" if _REACTION.fullmatch(t) else "question"


def fake_classification(text: str) -> Classification:
    topic = fake_topic(text)
    amount = _AMOUNT.search(text) and not re.search(r"שכיר|עצמאי", text)
    missing = "employment_status" if topic == "unemployment" and amount else None
    return Classification(
        topic=topic,
        in_scope=topic != "other",
        missing_fact=missing,
        office_lookup=asks_office(text),
        intent=fake_intent(text),
    )
