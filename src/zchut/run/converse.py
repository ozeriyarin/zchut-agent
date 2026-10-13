from __future__ import annotations

from zchut.graph.build import GraphDeps, Reply, converse
from zchut.graph.state import ConversationState
from zchut.run.cost_guard import CostGuard, Outcome, guarded
from zchut.run.records import question_hash
from zchut.types import GraphRecord, RetrievedRef, RunRecord


def record_state(record: RunRecord, deps: GraphDeps, state: ConversationState) -> None:
    record.question_hash = question_hash(state.question)
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
        escalated="escalate" in state.path,
        clarified=state.clarified,
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
