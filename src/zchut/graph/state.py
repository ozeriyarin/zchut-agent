from __future__ import annotations

import operator
from typing import Annotated, Literal

from pydantic import BaseModel

from zchut.types import Answer, CallRecord, Hit

Topic = Literal["unemployment", "pension", "tax", "other"]
Intent = Literal["question", "greeting", "reaction"]
HISTORY_CAP = 6


class Classification(BaseModel):
    topic: Topic
    in_scope: bool
    missing_fact: str | None = None
    office_lookup: bool = False
    intent: Intent = "question"


class Session(BaseModel):
    history: list[str] = []
    pending_clarification: str | None = None
    pending_fact: str | None = None
    clarified_once: bool = False


class Verdict(BaseModel):
    supported: bool
    unsupported_claims: list[str] = []


class ConversationState(BaseModel):
    question: str
    history: list[str] = []
    pending_clarification: str | None = None
    pending_fact: str | None = None
    standalone_question: str | None = None
    is_question: bool = True
    topic: Topic | None = None
    in_scope: bool | None = None
    missing_fact: str | None = None
    office_lookup: bool = False
    asks_office: bool = False
    intent: Intent = "question"
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

    @property
    def standalone(self) -> str:
        return self.standalone_question or self.question
