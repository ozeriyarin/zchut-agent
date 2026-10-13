from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from zchut.answer.refusal import should_refuse
from zchut.eval.metrics import citation_precision, hit_at_k, rate
from zchut.eval.questions import EvalQuestion
from zchut.run.ask import AskDeps, respond, retrieve
from zchut.run.cost_guard import CostGuard, Outcome, guarded


class EvalRow(BaseModel):
    id: str
    expected_urls: list[str]
    expect_refusal: bool
    retrieved_urls: list[str]
    refused: bool
    cited_urls: list[str]


class EvalReport(BaseModel):
    questions: int
    retrieval_only: bool
    hit_at_3: float | None
    citation_precision: float | None
    refusal_rate: float
    expected_refusal_rate: float
    rows: list[EvalRow]


def build_report(rows: Sequence[EvalRow], retrieval_only: bool) -> EvalReport:
    with_sources = [r for r in rows if r.expected_urls]
    hits = [hit_at_k(r.retrieved_urls, r.expected_urls, 3) for r in with_sources]
    return EvalReport(
        questions=len(rows),
        retrieval_only=retrieval_only,
        hit_at_3=rate(hits) if hits else None,
        citation_precision=citation_precision(
            [r.cited_urls for r in rows], [r.expected_urls for r in rows]
        ),
        refusal_rate=rate([r.refused for r in rows]),
        expected_refusal_rate=rate([r.expect_refusal for r in rows]),
        rows=list(rows),
    )


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def format_report(report: EvalReport) -> str:
    return "\n".join(
        [
            f"שאלות: {report.questions}",
            f"hit@3: {_fmt(report.hit_at_3)}",
            f"דיוק ציטוטים: {_fmt(report.citation_precision)}",
            f"שיעור סירוב: {_fmt(report.refusal_rate)}"
            f" (צפוי: {_fmt(report.expected_refusal_rate)})",
        ]
    )


def run_eval(
    questions: Sequence[EvalQuestion], deps: AskDeps, guard: CostGuard, retrieval_only: bool
) -> tuple[Outcome, EvalReport]:
    rows: list[EvalRow] = []
    record = guard.recorder.record

    def action() -> str:
        for q in questions:
            hits = retrieve(q.question, deps, guard)
            cited: list[str] = []
            if retrieval_only:
                refused = should_refuse(hits, deps.min_score)
            else:
                answer = respond(q.question, hits, deps, guard)
                refused = answer.refused
                record.dropped_citations += answer.dropped_citations
                cited = list(dict.fromkeys(c.chunk.url for c in answer.citations))
            rows.append(
                EvalRow(
                    id=q.id,
                    expected_urls=q.expected_urls,
                    expect_refusal=q.expect_refusal,
                    retrieved_urls=[h.chunk.url for h in hits],
                    refused=refused,
                    cited_urls=cited,
                )
            )
        return format_report(build_report(rows, retrieval_only))

    outcome = guarded(guard.recorder, action)
    return outcome, build_report(rows, retrieval_only)
