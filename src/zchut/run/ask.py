from __future__ import annotations

from dataclasses import dataclass

from zchut.answer.citations import render, resolve
from zchut.answer.model import AnswerModel
from zchut.answer.prompt import build_prompt
from zchut.answer.refusal import REFUSAL_SENTENCE, should_refuse
from zchut.index.embedder import Embedder
from zchut.index.store import VectorStore
from zchut.run.cost_guard import CostGuard, Outcome, guarded
from zchut.run.records import question_hash
from zchut.types import Answer, Hit, RetrievedRef


@dataclass(frozen=True)
class AskDeps:
    embedder: Embedder
    store: VectorStore
    model: AnswerModel
    k: int
    min_score: float


def retrieve(question: str, deps: AskDeps, guard: CostGuard) -> list[Hit]:
    [vector] = guard.embed(deps.embedder, [question], "query")
    return deps.store.search(vector, deps.k)


def respond(question: str, hits: list[Hit], deps: AskDeps, guard: CostGuard) -> Answer:
    if should_refuse(hits, deps.min_score):
        return Answer(text=REFUSAL_SENTENCE, citations=(), refused=True)
    return resolve(guard.answer(deps.model, build_prompt(question, hits)), hits)


def run_ask(question: str, deps: AskDeps, guard: CostGuard) -> Outcome:
    record = guard.recorder.record
    record.question_hash = question_hash(question)

    def action() -> str:
        hits = retrieve(question, deps, guard)
        record.retrieved = [RetrievedRef(chunk_id=h.chunk.id, score=h.score) for h in hits]
        answer = respond(question, hits, deps, guard)
        record.refused = answer.refused
        record.dropped_citations = answer.dropped_citations
        return answer.text if answer.refused else render(answer)

    return guarded(guard.recorder, action)
