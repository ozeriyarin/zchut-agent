from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from zchut.config import ConfigError
from zchut.graph.build import GraphDeps
from zchut.graph.clarifications import Clarifications
from zchut.graph.offices import Office, office_line
from zchut.graph.state import ConversationState, Session, Topic
from zchut.run.converse import take_turn
from zchut.run.cost_guard import CostGuard, Outcome, guarded


class ConversationTurn(BaseModel):
    message: str = Field(min_length=1)
    expect: str = Field(pattern=r"^(?:answer|nudge|office|refuse|clarify:[a-z_]+)$")
    expected_urls: list[str] = []


class EvalConversation(BaseModel):
    id: str = Field(min_length=1)
    turns: list[ConversationTurn] = Field(min_length=1)


class TurnRow(BaseModel):
    id: str
    expect: str
    outcome: str
    cited_urls: list[str]
    correct: bool


class ConversationReport(BaseModel):
    conversations: int
    turns: int
    turn_accuracy: float
    rows: list[TurnRow]


_LIST = TypeAdapter(list[EvalConversation])


def load_conversations(path: Path, clarifications: Clarifications) -> list[EvalConversation]:
    try:
        found = _LIST.validate_python(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise ConfigError(f"invalid {path}: {exc}") from exc
    keys = {t.expect.split(":")[1] for c in found for t in c.turns if ":" in t.expect}
    if unknown := sorted(keys - set(clarifications)):
        raise ConfigError(f"{path}: unknown clarification keys {unknown}")
    return found


def turn_outcome(state: ConversationState) -> str:
    if state.final is None:
        return f"clarify:{state.missing_fact}"
    for outcome in ("nudge", "refuse"):
        if outcome in state.path:
            return outcome
    if state.escalation is not None:
        return "escalate"
    return "office" if state.office_lookup or state.asks_office else "answer"


def _row(
    row_id: str, turn: ConversationTurn, state: ConversationState, offices: dict[Topic, Office]
) -> TurnRow:
    shown = state.draft.citations if state.draft and state.escalation is None else ()
    cited = list(dict.fromkeys(c.chunk.url for c in shown))
    outcome = turn_outcome(state)
    sourced = not turn.expected_urls or bool(set(cited) & set(turn.expected_urls))
    line = office_line(offices[state.topic or "other"])
    listed = turn.expect != "office" or line in (state.final or "")
    return TurnRow(
        id=row_id,
        expect=turn.expect,
        outcome=outcome,
        cited_urls=cited,
        correct=outcome == turn.expect and sourced and listed,
    )


def format_conversations(report: ConversationReport) -> str:
    right = sum(r.correct for r in report.rows)
    lines = [
        f"שיחות: {report.conversations}, תורות: {report.turns}",
        f"דיוק תורות: {report.turn_accuracy:.2f} ({right}/{report.turns})",
    ]
    marks = [f"{r.id} {r.expect} -> {r.outcome} {'✓' if r.correct else '✗'}" for r in report.rows]
    return "\n".join([*lines, *marks])


def run_conversations(
    conversations: Sequence[EvalConversation], deps: GraphDeps, guard: CostGuard
) -> tuple[Outcome, list[ConversationReport]]:
    reports: list[ConversationReport] = []

    def action() -> str:
        rows: list[TurnRow] = []
        for conversation in conversations:
            session = Session()
            for n, turn in enumerate(conversation.turns, start=1):
                state, session = take_turn(deps, guard, turn.message, session)
                rows.append(_row(f"{conversation.id}.{n}", turn, state, deps.offices))
        accuracy = sum(r.correct for r in rows) / len(rows) if rows else 0.0
        report = ConversationReport(
            conversations=len(conversations), turns=len(rows), turn_accuracy=accuracy, rows=rows
        )
        reports.append(report)
        return format_conversations(report)

    return guarded(guard.recorder, action), reports
