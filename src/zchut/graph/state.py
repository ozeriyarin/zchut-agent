from __future__ import annotations

import operator
from typing import Annotated, Literal

from pydantic import BaseModel

from zchut.types import Answer, CallRecord, Hit

Topic = Literal["unemployment", "pension", "tax", "other"]
HISTORY_CAP = 6


class Classification(BaseModel):
    topic: Topic
    in_scope: bool
    missing_fact: str | None = None


class Verdict(BaseModel):
    supported: bool
    unsupported_claims: list[str] = []


class ConversationState(BaseModel):
    question: str
    history: list[str] = []
    topic: Topic | None = None
    in_scope: bool | None = None
    missing_fact: str | None = None
    clarified: bool = False
    can_clarify: bool = True
    hits: list[Hit] = []
    draft: Answer | None = None
    verdict: Verdict | None = None
    final: str | None = None
    escalation: str | None = None
    path: Annotated[list[str], operator.add] = []
    calls: Annotated[list[CallRecord], operator.add] = []

    @property
    def verified(self) -> bool | None:
        return None if self.verdict is None else self.verdict.supported

    def query(self) -> str:
        return f"{self.question} {self.history[-1]}" if self.clarified else self.question

    def conversation(self) -> str:
        return "\n".join([*self.history, self.question])
