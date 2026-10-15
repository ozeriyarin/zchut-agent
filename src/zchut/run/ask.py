from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from zchut.answer.citations import render, resolve
from zchut.answer.model import AnswerModel
from zchut.answer.prompt import build_prompt
from zchut.answer.refusal import REFUSAL_SENTENCE, should_refuse
from zchut.answer.render import clean_answer
from zchut.index.embedder import Embedder
from zchut.index.keyword import FUSION_POOL, KeywordIndex, fuse
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
    keyword: KeywordIndex | None = None


def search(question: str, vector: Sequence[float], deps: AskDeps) -> list[Hit]:
    if deps.keyword is None:
        return deps.store.search(vector, deps.k)
    pool = deps.store.search(vector, max(FUSION_POOL, deps.k))
    return fuse(pool, deps.keyword.search(question, FUSION_POOL), deps.k)


def retrieve(question: str, deps: AskDeps, guard: CostGuard) -> list[Hit]:
    [vector] = guard.embed(deps.embedder, [question], "query")
    return search(question, vector, deps)


def respond(question: str, hits: list[Hit], deps: AskDeps, guard: CostGuard) -> Answer:
    if should_refuse(hits, deps.min_score):
        return Answer(text=REFUSAL_SENTENCE, citations=(), refused=True)
    raw = guard.answer(deps.model, build_prompt(question, hits))
    text, stripped, scrubbed = clean_answer(raw)
    return replace(resolve(text, hits), markdown_stripped=stripped, scrubbed=scrubbed)


def run_ask(question: str, deps: AskDeps, guard: CostGuard) -> Outcome:
    record = guard.recorder.record
    record.question_hash = question_hash(question)

    def action() -> str:
        hits = retrieve(question, deps, guard)
        record.retrieved = [RetrievedRef(chunk_id=h.chunk.id, score=h.score) for h in hits]
        answer = respond(question, hits, deps, guard)
        record.refused = answer.refused
        record.dropped_citations = answer.dropped_citations
        record.markdown_stripped = answer.markdown_stripped
        record.scrubbed = answer.scrubbed
        return answer.text if answer.refused else render(answer)

    return guarded(guard.recorder, action)
