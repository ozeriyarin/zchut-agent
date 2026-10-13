from __future__ import annotations

from collections.abc import Sequence
from statistics import median

from pydantic import BaseModel

from zchut.answer.refusal import should_refuse
from zchut.eval.metrics import citation_precision, hit_at_k, rate
from zchut.eval.questions import EvalQuestion
from zchut.run.ask import AskDeps, respond
from zchut.run.cost_guard import CostGuard, Outcome, guarded


class EvalRow(BaseModel):
    id: str
    expected_urls: list[str]
    expect_refusal: bool
    retrieved_urls: list[str]
    retrieved_scores: list[float]
    best_score: float | None
    hit: bool | None
    refused: bool
    cited_urls: list[str]


class Calibration(BaseModel):
    answerable_min_best_score: float | None
    answerable_median: float | None
    refusal_max_best_score: float | None
    refusal_median: float | None
    suggested_min_score: float | None
    overlap: bool


class EvalReport(BaseModel):
    questions: int
    retrieval_only: bool
    hit_at_3: float | None
    citation_precision: float | None
    refusal_rate: float
    expected_refusal_rate: float
    calibration: Calibration
    rows: list[EvalRow]


def calibrate(answerable: Sequence[float], refusal: Sequence[float]) -> Calibration:
    low = min(answerable, default=None)
    high = max(refusal, default=None)
    overlap = low is not None and high is not None and high >= low
    suggested = None if low is None or high is None or overlap else (low + high) / 2
    return Calibration(
        answerable_min_best_score=low,
        answerable_median=median(answerable) if answerable else None,
        refusal_max_best_score=high,
        refusal_median=median(refusal) if refusal else None,
        suggested_min_score=suggested,
        overlap=overlap,
    )


def build_report(rows: Sequence[EvalRow], retrieval_only: bool) -> EvalReport:
    hits = [r.hit for r in rows if r.hit is not None]
    scored = [(r, r.best_score) for r in rows if r.best_score is not None]
    return EvalReport(
        questions=len(rows),
        retrieval_only=retrieval_only,
        hit_at_3=rate(hits) if hits else None,
        citation_precision=citation_precision(
            [r.cited_urls for r in rows], [r.expected_urls for r in rows]
        ),
        refusal_rate=rate([r.refused for r in rows]),
        expected_refusal_rate=rate([r.expect_refusal for r in rows]),
        calibration=calibrate(
            [s for r, s in scored if r.expected_urls and not r.expect_refusal],
            [s for r, s in scored if r.expect_refusal],
        ),
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
            _calibration_line(report.calibration),
        ]
    )


def _calibration_line(c: Calibration) -> str:
    bounds = (
        f"שאלות עם מקור ≥ {_fmt(c.answerable_min_best_score)}, "
        f"שאלות לסירוב ≤ {_fmt(c.refusal_max_best_score)}"
    )
    if c.overlap:
        return f"כיול: {bounds} → חפיפה, אין סף שמפריד ביניהן"
    return f"כיול: {bounds} → סף מוצע {_fmt(c.suggested_min_score)}"


def run_eval(
    questions: Sequence[EvalQuestion], deps: AskDeps, guard: CostGuard, retrieval_only: bool
) -> tuple[Outcome, EvalReport]:
    rows: list[EvalRow] = []
    record = guard.recorder.record

    def action() -> str:
        vectors = guard.embed(deps.embedder, [q.question for q in questions], "query")
        for q, vector in zip(questions, vectors, strict=True):
            hits = deps.store.search(vector, deps.k)
            urls = [h.chunk.url for h in hits]
            scores = [h.score for h in hits]
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
                    retrieved_urls=urls,
                    retrieved_scores=scores,
                    best_score=max(scores, default=None),
                    hit=hit_at_k(urls, q.expected_urls, 3) if q.expected_urls else None,
                    refused=refused,
                    cited_urls=cited,
                )
            )
        return format_report(build_report(rows, retrieval_only))

    outcome = guarded(guard.recorder, action)
    return outcome, build_report(rows, retrieval_only)
