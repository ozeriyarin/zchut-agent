from __future__ import annotations

import re
from typing import Protocol, get_args

import anthropic
from pydantic import ValidationError

from zchut.answer.model import AnswerError, haiku_json
from zchut.graph.state import Classification, Topic
from zchut.run.cost_guard import CostGuard
from zchut.tokens import estimate_tokens
from zchut.types import Usage

FAKE_CLASSIFIER_MODEL = "fake-classifier"
CLASSIFY_SYSTEM = (
    "אתה מסווג שאלות על זכויות ובירוקרטיה בישראל. החזר JSON בלבד לפי הסכמה. "
    "topic: unemployment לדמי אבטלה ולשירות התעסוקה, pension לפנסיה ולחיסכון, "
    "tax למס הכנסה ולהחזרי מס, other לכל נושא אחר. "
    "in_scope: true רק כשהשאלה עוסקת בזכויות מול ביטוח לאומי, שירות התעסוקה, פנסיה או מס. "
    "missing_fact: שאלת הבהרה אחת קצרה בעברית, רק כשאי אפשר לענות בלי עובדה שהמשתמש יודע "
    "(למשל שכיר או עצמאי, טווח גיל); אחרת null."
)
CLASSIFY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "enum": list(get_args(Topic))},
        "in_scope": {"type": "boolean"},
        "missing_fact": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["topic", "in_scope", "missing_fact"],
    "additionalProperties": False,
}
EMPLOYMENT_QUESTION = "האם עבדת כשכיר/ה או כעצמאי/ת?"
_TOPIC_WORDS: list[tuple[Topic, re.Pattern[str]]] = [
    ("unemployment", re.compile(r"אבטלה|תעסוקה|פיטורי|התפטר")),
    ("pension", re.compile(r"פנסי|הפרש")),
    ("tax", re.compile(r"(?<!\w)[הלב]?מס(?!\w)|טופס 106")),
]


class Classifier(Protocol):
    model: str
    max_tokens: int

    def classify(self, text: str, guard: CostGuard) -> Classification: ...


class HaikuClassifier:
    def __init__(self, client: anthropic.Anthropic, model: str, max_tokens: int) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    def classify(self, text: str, guard: CostGuard) -> Classification:
        def send() -> tuple[Classification, Usage]:
            data, usage = haiku_json(
                self._client, self.model, self.max_tokens, CLASSIFY_SYSTEM, text, CLASSIFY_SCHEMA
            )
            try:
                return Classification.model_validate(data), usage
            except ValidationError as exc:
                raise AnswerError(f"{self.model} returned an invalid classification") from exc

        return guard.call("classify", self.model, CLASSIFY_SYSTEM + text, self.max_tokens, send)


class FakeClassifier:
    def __init__(self, max_tokens: int = 200) -> None:
        self.model = FAKE_CLASSIFIER_MODEL
        self.max_tokens = max_tokens

    def classify(self, text: str, guard: CostGuard) -> Classification:
        def send() -> tuple[Classification, Usage]:
            result = fake_classification(text)
            return result, Usage(estimate_tokens(text), estimate_tokens(result.model_dump_json()))

        return guard.call("classify", self.model, text, self.max_tokens, send)


def fake_classification(text: str) -> Classification:
    topic: Topic = next((t for t, words in _TOPIC_WORDS if words.search(text)), "other")
    amount = re.search(r"כמה אקבל|גובה", text) and not re.search(r"שכיר|עצמאי", text)
    missing = EMPLOYMENT_QUESTION if topic == "unemployment" and amount else None
    return Classification(topic=topic, in_scope=topic != "other", missing_fact=missing)
