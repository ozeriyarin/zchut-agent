from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from zchut.config import ConfigError

QUESTION_COUNT = 30


class EvalQuestion(BaseModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expected_urls: list[str] = []
    expect_refusal: bool = False

    @model_validator(mode="after")
    def _has_expectation(self) -> EvalQuestion:
        if not self.expected_urls and not self.expect_refusal:
            raise ValueError(f"{self.id}: needs expected_urls or expect_refusal: true")
        return self


_LIST = TypeAdapter(list[EvalQuestion])


def load_questions(path: Path) -> list[EvalQuestion]:
    try:
        questions = _LIST.validate_python(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise ConfigError(f"invalid {path}: {exc}") from exc
    if len(questions) != QUESTION_COUNT:
        raise ConfigError(f"expected {QUESTION_COUNT} questions, found {len(questions)}")
    if len({q.id for q in questions}) != len(questions):
        raise ConfigError("question ids must be unique")
    return questions
