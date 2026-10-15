from __future__ import annotations

from zchut.graph.build import GraphDeps, Reply, converse
from zchut.graph.state import HISTORY_CAP, ConversationState, Session
from zchut.run.cost_guard import CostGuard, Outcome, guarded
from zchut.run.records import question_hash
from zchut.types import GraphRecord, RetrievedRef, RunRecord


def record_state(record: RunRecord, deps: GraphDeps, state: ConversationState) -> None:
    record.question_hash = question_hash(state.standalone)
    if state.history:
        record.history_hash = question_hash("\n".join(state.history))
    record.retrieved = [RetrievedRef(chunk_id=h.chunk.id, score=h.score) for h in state.hits]
    record.refused = "refuse" in state.path
    record.dropped_citations = state.draft.dropped_citations if state.draft else 0
    record.markdown_stripped = state.draft.markdown_stripped if state.draft else 0
    record.graph = GraphRecord(
        path=state.path,
        tier=deps.tier,
        verifier=deps.verifier.tier if deps.verifier else None,
        verified=state.verified,
        escalated=state.escalation is not None,
        clarified=state.clarified,
        office_lookup=state.office_lookup,
    )


def run_conversation(
    question: str, history: list[str], deps: GraphDeps, guard: CostGuard, reply: Reply | None
) -> Outcome:
    guard.recorder.record.question_hash = question_hash(question)

    def action() -> str:
        state = converse(deps, guard, question, history, reply)
        record_state(guard.recorder.record, deps, state)
        return state.final or ""

    return guarded(guard.recorder, action)


def take_turn(
    deps: GraphDeps, guard: CostGuard, text: str, session: Session
) -> tuple[ConversationState, Session]:
    state = converse(deps, guard, text, session.history, None, pause=True, session=session)
    if state.final is None:
        asked = {"pending_fact": state.missing_fact, "clarified_once": True}
        return state, session.model_copy(
            update={"pending_clarification": state.standalone, **asked}
        )
    if "nudge" in state.path:
        return state, session
    history = [*session.history, state.standalone][-HISTORY_CAP:]
    return state, Session(history=history, clarified_once=session.clarified_once)


def turn_text(deps: GraphDeps, state: ConversationState) -> str:
    if state.final is not None:
        return state.final
    assert state.missing_fact is not None
    return deps.clarifications[state.missing_fact].question_he


def run_turn(
    text: str, session: Session, deps: GraphDeps, guard: CostGuard
) -> tuple[Outcome, ConversationState | None, Session]:
    guard.recorder.record.question_hash = question_hash(text)
    turns: list[tuple[ConversationState, Session]] = []

    def action() -> str:
        state, after = take_turn(deps, guard, text, session)
        record_state(guard.recorder.record, deps, state)
        turns.append((state, after))
        return turn_text(deps, state)

    outcome = guarded(guard.recorder, action)
    state, after = turns[0] if turns else (None, session)
    return outcome, state, after
