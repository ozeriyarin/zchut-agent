from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from statistics import fmean, median

from pydantic import BaseModel

from zchut.answer.refusal import should_refuse
from zchut.eval.labels import VerifierLabel, verifier_accuracy
from zchut.eval.metrics import citation_precision, hit_at_k, rate
from zchut.eval.questions import EvalQuestion
from zchut.graph.build import GraphDeps, converse
from zchut.run.ask import AskDeps, search
from zchut.run.cost_guard import CostGuard, Outcome, guarded
from zchut.types import Hit


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
    path: list[str] = []
    verified: bool | None = None
    escalated: bool = False
    markdown_stripped: int | None = None
    cost_usd: float = 0.0
    latency_ms: int = 0


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
    tier: str | None = None
    verifier: str | None = None
    hit_at_3: float | None
    citation_precision: float | None
    refusal_rate: float
    expected_refusal_rate: float
    verification_pass_rate: float | None = None
    verifier_accuracy: float | None = None
    escalation_rate: float | None = None
    markdown_free: float | None = None
    mean_cost_usd: float = 0.0
    mean_latency_ms: float = 0.0
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


def build_report(
    rows: Sequence[EvalRow],
    combo: GraphDeps | None = None,
    labels: Mapping[str, VerifierLabel] | None = None,
) -> EvalReport:
    hits = [r.hit for r in rows if r.hit is not None]
    scored = [(r, r.best_score) for r in rows if r.best_score is not None]
    verdicts = {r.id: r.verified for r in rows if r.verified is not None}
    drafts = [r.markdown_stripped == 0 for r in rows if r.markdown_stripped is not None]
    verifier = combo.verifier if combo else None
    return EvalReport(
        questions=len(rows),
        retrieval_only=combo is None,
        tier=combo.tier if combo else None,
        verifier=verifier.tier if verifier else None,
        hit_at_3=rate(hits) if hits else None,
        citation_precision=citation_precision(
            [r.cited_urls for r in rows], [r.expected_urls for r in rows]
        ),
        refusal_rate=rate([r.refused for r in rows]),
        expected_refusal_rate=rate([r.expect_refusal for r in rows]),
        verification_pass_rate=rate(list(verdicts.values())) if verdicts else None,
        verifier_accuracy=verifier_accuracy(verdicts, labels or {}),
        escalation_rate=None if combo is None else rate([r.escalated for r in rows]),
        markdown_free=rate(drafts) if drafts else None,
        mean_cost_usd=fmean([r.cost_usd for r in rows]) if rows else 0.0,
        mean_latency_ms=fmean([r.latency_ms for r in rows]) if rows else 0.0,
        calibration=calibrate(
            [s for r, s in scored if r.expected_urls and not r.expect_refusal],
            [s for r, s in scored if r.expect_refusal],
        ),
        rows=list(rows),
    )


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def format_report(report: EvalReport) -> str:
    title = [f"שכבה: {report.tier}, מאמת: {report.verifier or 'ללא'}"] if report.tier else []
    return "\n".join(
        [
            *title,
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


def format_table(reports: Sequence[EvalReport]) -> str:
    lines = [
        "| tier | verifier | hit@3 | citation precision | refusal rate | verification pass rate"
        " | verifier accuracy | escalation rate | markdown free | cost / question (USD)"
        " | latency (ms) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in reports:
        cells = [
            r.tier or "—",
            r.verifier or "—",
            _fmt(r.hit_at_3),
            _fmt(r.citation_precision),
            _fmt(r.refusal_rate),
            _fmt(r.verification_pass_rate),
            _fmt(r.verifier_accuracy),
            _fmt(r.escalation_rate),
            _fmt(r.markdown_free),
            f"{r.mean_cost_usd:.5f}",
            f"{r.mean_latency_ms:.0f}",
        ]
        lines.append(f"| {' | '.join(cells)} |")
    return "\n".join(lines)


def _row(q: EvalQuestion, hits: Sequence[Hit], refused: bool, **extra: object) -> EvalRow:
    urls = [h.chunk.url for h in hits]
    scores = [h.score for h in hits]
    return EvalRow.model_validate(
        {
            "id": q.id,
            "expected_urls": q.expected_urls,
            "expect_refusal": q.expect_refusal,
            "retrieved_urls": urls,
            "retrieved_scores": scores,
            "best_score": max(scores, default=None),
            "hit": hit_at_k(urls, q.expected_urls, 3) if q.expected_urls else None,
            "refused": refused,
            "cited_urls": [],
            **extra,
        }
    )


def _graph_row(q: EvalQuestion, hits: list[Hit], combo: GraphDeps, guard: CostGuard) -> EvalRow:
    record = guard.recorder.record
    before, started = len(record.calls), time.perf_counter()
    state = converse(replace(combo, retriever=lambda _q, _g: hits), guard, q.question, [], None)
    latency_ms = int((time.perf_counter() - started) * 1000)
    shown = state.draft if state.escalation is None else None
    drafted = state.draft if state.draft and not state.draft.refused else None
    if state.draft:
        record.dropped_citations += state.draft.dropped_citations
    return _row(
        q,
        hits,
        "refuse" in state.path,
        cited_urls=list(dict.fromkeys(c.chunk.url for c in shown.citations)) if shown else [],
        path=state.path,
        verified=state.verified,
        escalated=state.escalation is not None,
        markdown_stripped=drafted.markdown_stripped if drafted else None,
        cost_usd=sum(c.cost_usd for c in record.calls[before:]),
        latency_ms=latency_ms,
    )


def run_eval(
    questions: Sequence[EvalQuestion],
    deps: AskDeps,
    guard: CostGuard,
    combos: Sequence[GraphDeps],
    labels: Mapping[str, VerifierLabel] | None = None,
) -> tuple[Outcome, list[EvalReport]]:
    reports: list[EvalReport] = []

    def action() -> str:
        vectors = guard.embed(deps.embedder, [q.question for q in questions], "query")
        found = [(q, search(q.question, v, deps)) for q, v in zip(questions, vectors, strict=True)]
        if not combos:
            rows = [_row(q, hits, should_refuse(hits, deps.min_score)) for q, hits in found]
            reports.append(build_report(rows))
        for combo in combos:
            rows = [_graph_row(q, h, combo, guard) for q, h in found]
            reports.append(build_report(rows, combo, labels))
        blocks = [format_report(r) for r in reports]
        return "\n\n".join([*blocks, format_table(reports)] if combos else blocks)

    return guarded(guard.recorder, action), reports
