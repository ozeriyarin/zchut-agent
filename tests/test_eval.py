from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import AVTALA, PENSIA, ROOT, TAX, make_deps, make_graph_deps, make_guard
from zchut.answer.model import FakeAnswerModel
from zchut.cli import app
from zchut.config import ConfigError, Prices, Settings, load_settings
from zchut.eval.labels import VerifierLabel, load_labels, verifier_accuracy
from zchut.eval.metrics import citation_precision, hit_at_k, rate
from zchut.eval.questions import EvalQuestion, load_questions
from zchut.eval.run_eval import _calibration_line, calibrate, format_table, run_eval
from zchut.graph.verify import VERIFY_SCHEMA, VERIFY_SYSTEM, FakeVerifier
from zchut.index.embedder import FAKE_EMBED_MODEL, FakeEmbedder, InputType
from zchut.index.store import InMemoryVectorStore
from zchut.run.ask import AskDeps
from zchut.run.cost_guard import cost_usd
from zchut.tokens import estimate_tokens
from zchut.types import EmbedResult, Usage

runner = CliRunner()
FIXTURE_URLS = {AVTALA, TAX, PENSIA}


def test_criterion_7_eval_set_and_dry_run_report(cli_root: Path) -> None:
    """criterion 7: "`eval/questions.json` holds exactly 30 entries that validate against the schema (`id`, `question`, at least one `expected_urls` entry or `expect_refusal: true`), and `zchut eval --dry-run` over the fixture index reports retrieval hit@3, citation precision and refusal rate and writes an eval report under `runs/`." """
    questions = load_questions(ROOT / "eval" / "questions.json")
    assert len(questions) == 30
    known = FIXTURE_URLS | set(load_settings(ROOT).sources)
    for q in questions:
        urls = set(q.expected_urls)
        assert q.expect_refusal or (urls & FIXTURE_URLS and urls <= known)
    with pytest.raises(ValueError, match="expected_urls or expect_refusal"):
        EvalQuestion(id="x", question="שאלה?")

    result = runner.invoke(app, ["eval", "--dry-run"])
    assert result.exit_code == 0, result.output
    for label in ("hit@3:", "דיוק ציטוטים:", "שיעור סירוב:", "כיול: שאלות עם מקור ≥"):
        assert label in result.output
    report = json.loads(next((cli_root / "runs").glob("*-eval-report.json")).read_text("utf-8"))
    assert report["questions"] == 30 and len(report["rows"]) == 30
    for row in report["rows"]:
        assert row["best_score"] == max(row["retrieved_scores"])
        assert row["expected_urls"] or row["hit"] is None
    sourced = [r for r in report["rows"] if r["expected_urls"]]
    assert all(isinstance(r["hit"], bool) for r in sourced)
    assert report["hit_at_3"] == pytest.approx(sum(r["hit"] for r in sourced) / len(sourced))
    cal = report["calibration"]
    answerable = [r["best_score"] for r in sourced]
    refusal = [r["best_score"] for r in report["rows"] if r["expect_refusal"]]
    assert cal["answerable_min_best_score"] == min(answerable)
    assert cal["refusal_max_best_score"] == max(refusal)
    assert cal["overlap"] is (max(refusal) >= min(answerable))
    assert (cal["suggested_min_score"] is None) is cal["overlap"]
    assert report["hit_at_3"] is not None and report["citation_precision"] is not None
    assert report["expected_refusal_rate"] == pytest.approx(6 / 30)
    assert next((cli_root / "runs").glob("*-eval.json"))


def test_load_questions_rejects_wrong_count_and_duplicates(tmp_path: Path) -> None:
    row = {"id": "q", "question": "שאלה?", "expect_refusal": True}
    path = tmp_path / "q.json"
    path.write_text(json.dumps([row] * 29), "utf-8")
    with pytest.raises(ConfigError, match="expected 30"):
        load_questions(path)
    path.write_text(json.dumps([row] * 30), "utf-8")
    with pytest.raises(ConfigError, match="unique"):
        load_questions(path)


def test_run_eval_retrieval_only_makes_no_answer_calls(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    questions = load_questions(ROOT / "eval" / "questions.json")[:3]
    model = FakeAnswerModel()
    guard = make_guard(priced)
    outcome, [report] = run_eval(questions, make_deps(fixture_store, model, 0.3), guard, [])
    assert outcome.exit_code == 0 and model.calls == 0
    assert report.hit_at_3 == 1.0 and report.citation_precision is None
    assert {c.role for c in guard.recorder.record.calls} == {"embed"}


@pytest.mark.parametrize(
    ("retrieved", "expected", "hit"),
    [
        (["a", "b", "c", "d"], {"c"}, True),
        (["a", "b", "c", "d"], {"d"}, False),
        ([], {"a"}, False),
    ],
)
def test_hit_at_k(retrieved: list[str], expected: set[str], hit: bool) -> None:
    assert hit_at_k(retrieved, expected, 3) is hit


@pytest.mark.parametrize(
    ("cited", "expected", "precision"),
    [
        ([["a"], ["b", "c"]], [{"a"}, {"b"}], 2 / 3),
        ([[], []], [{"a"}, set()], None),
        ([["a"]], [set()], 0.0),
    ],
)
def test_citation_precision(
    cited: list[list[str]], expected: list[set[str]], precision: float | None
) -> None:
    assert citation_precision(cited, expected) == precision


def test_rate() -> None:
    assert rate([True, False, False, True]) == 0.5 and rate([]) == 0.0


@pytest.mark.parametrize(
    ("answerable", "refusal", "suggested", "overlap"),
    [
        ([0.6, 0.52, 0.7], [0.47, 0.3, 0.4], 0.495, False),
        ([0.55, 0.6], [0.58, 0.4], None, True),
        ([0.5], [0.5], None, True),
        ([0.5], [], None, False),
    ],
)
def test_calibrate(
    answerable: list[float], refusal: list[float], suggested: float | None, overlap: bool
) -> None:
    cal = calibrate(answerable, refusal)
    assert cal.overlap is overlap
    assert cal.suggested_min_score == (None if suggested is None else pytest.approx(suggested))
    assert cal.answerable_min_best_score == min(answerable)
    assert cal.refusal_max_best_score == max(refusal, default=None)


def test_calibrate_medians() -> None:
    cal = calibrate([0.5, 0.7, 0.6], [0.2, 0.4])
    assert cal.answerable_median == pytest.approx(0.6)
    assert cal.refusal_median == pytest.approx(0.3)


def test_calibration_line() -> None:
    line = _calibration_line(calibrate([0.52, 0.6], [0.48, 0.2]))
    assert line == "כיול: שאלות עם מקור ≥ 0.52, שאלות לסירוב ≤ 0.48 → סף מוצע 0.50"
    assert "חפיפה" in _calibration_line(calibrate([0.4], [0.5]))


class SpyEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[int] = []

    def embed(self, texts: Sequence[str], input_type: InputType) -> EmbedResult:
        self.batches.append(len(texts))
        return super().embed(texts, input_type)


def test_run_eval_embeds_all_questions_in_one_call(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    questions = load_questions(ROOT / "eval" / "questions.json")
    spy = SpyEmbedder()
    model = FakeAnswerModel()
    guard = make_guard(priced)
    outcome, [report] = run_eval(questions, AskDeps(spy, fixture_store, model, 5, 0.3), guard, [])
    assert outcome.exit_code == 0 and spy.batches == [30] and model.calls == 0
    assert [c.role for c in guard.recorder.record.calls] == ["embed"]
    assert len(report.rows) == 30


def test_run_eval_budget_covers_the_whole_query_batch(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    questions = load_questions(ROOT / "eval" / "questions.json")
    tokens = sum(estimate_tokens(q.question) for q in questions)
    estimate = cost_usd(priced, FAKE_EMBED_MODEL, Usage(tokens))
    spy = SpyEmbedder()
    guard = make_guard(priced, estimate * 0.99)
    outcome, _ = run_eval(
        questions, AskDeps(spy, fixture_store, FakeAnswerModel(), 5, 0.3), guard, []
    )
    assert outcome.exit_code == 2 and spy.batches == []
    outcome, _ = run_eval(
        questions,
        AskDeps(spy, fixture_store, FakeAnswerModel(), 5, 0.3),
        make_guard(priced, estimate),
        [],
    )
    assert outcome.exit_code == 0 and spy.batches == [30]


def test_criterion_5_eval_per_tier_with_side_by_side_table(cli_root: Path) -> None:
    """criterion 5: "`zchut eval --tiers haiku,qwen-openrouter --dry-run` writes one report per tier plus a side-by-side table with verification pass rate, escalation rate, cost per question and latency per tier." """
    result = runner.invoke(app, ["eval", "--tiers", "haiku,qwen-openrouter", "--dry-run"])
    assert result.exit_code == 0, result.output
    runs = cli_root / "runs"
    reports = [json.loads(p.read_text("utf-8")) for p in sorted(runs.glob("*-report.json"))]
    assert [(r["tier"], r["verifier"]) for r in reports] == [
        ("haiku", "haiku"),
        ("qwen-openrouter", "haiku"),
    ]
    for report in reports:
        assert report["questions"] == 30 and report["verification_pass_rate"] is not None
        assert report["escalation_rate"] is not None
        assert report["mean_cost_usd"] == 0 and report["mean_latency_ms"] >= 0
        assert all("verify" in r["path"] for r in report["rows"] if not r["refused"])
    table = next(runs.glob("*-comparison.md")).read_text("utf-8").splitlines()
    for column in ("verification pass rate", "escalation rate", "cost / question", "latency"):
        assert column in table[0]
    assert [line.split(" | ")[:2] for line in table[2:]] == [
        ["| haiku", "haiku"],
        ["| qwen-openrouter", "haiku"],
    ]
    assert "\n".join(table) in result.output


def test_graph_eval_costs_add_up_per_question(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    questions = load_questions(ROOT / "eval" / "questions.json")[:4]
    guard = make_guard(priced)
    combos = [
        make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier()),
        make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, None, "vllm"),
    ]
    outcome, reports = run_eval(questions, combos[0].ask, guard, combos)
    assert outcome.exit_code == 0 and [r.tier for r in reports] == ["haiku", "vllm"]
    record = guard.recorder.record
    assert [c.role for c in record.calls].count("embed") == 1
    rows = [row for r in reports for row in r.rows]
    embed = record.calls[0].cost_usd
    assert sum(r.cost_usd for r in rows) + embed == pytest.approx(record.total_cost_usd)
    assert reports[0].mean_cost_usd > 0 and reports[0].verification_pass_rate == 1.0
    assert reports[1].verification_pass_rate is None and reports[1].escalation_rate == 0.0


def _column(table: str, name: str) -> list[str]:
    head, _, *rows = table.splitlines()
    index = [c.strip() for c in head.split("|")].index(name)
    return [[c.strip() for c in row.split("|")][index] for row in rows]


def _table(output: str) -> str:
    return "\n".join(line for line in output.splitlines() if line.startswith("|"))


def test_criterion_4_v4a_verifier_accuracy_column(cli_root: Path) -> None:
    """criterion 4: "With `eval/verifier_labels.json` present, the eval table has a `verifier accuracy` column per verifier computed against the labels; without the file the column is `—`." """
    path = cli_root / "eval" / "verifier_labels.json"
    assert load_labels(path) == {}
    shipped = runner.invoke(app, ["eval", "--dry-run"])
    assert shipped.exit_code == 0, shipped.output
    assert _column(_table(shipped.output), "verifier accuracy") == ["—"]
    path.unlink()
    missing = runner.invoke(app, ["eval", "--dry-run"])
    assert _column(_table(missing.output), "verifier accuracy") == ["—"]

    questions = load_questions(ROOT / "eval" / "questions.json")
    labels = {
        q.id: {"label": "supported" if n % 2 else "unsupported", "reason": "נבדק מול הקטע"}
        for n, q in enumerate(questions)
    }
    path.write_text(json.dumps({"version": 1, "labels": labels}), "utf-8")
    labelled = runner.invoke(app, ["eval", "--dry-run"])
    assert labelled.exit_code == 0, labelled.output
    report_path = labelled.output.split("דוח הערכה: ", 1)[1].splitlines()[0]
    report = json.loads(Path(report_path).read_text("utf-8"))
    judged = [
        r["verified"] == (labels[r["id"]]["label"] == "supported")
        for r in report["rows"]
        if r["verified"] is not None
    ]
    assert judged and report["verifier_accuracy"] == pytest.approx(sum(judged) / len(judged))
    cell = f"{report['verifier_accuracy']:.2f}"
    assert _column(_table(labelled.output), "verifier accuracy") == [cell]


def _label(label: str) -> VerifierLabel:
    return VerifierLabel.model_validate({"label": label, "reason": "סיבה"})


@pytest.mark.parametrize(
    ("verdicts", "labels", "accuracy"),
    [
        ({"q1": True, "q2": False}, {"q1": "supported", "q2": "supported"}, 0.5),
        ({"q1": False, "q2": True}, {"q1": "unsupported"}, 1.0),
        ({"q1": True}, {}, None),
        ({"q1": True}, {"q9": "supported"}, None),
    ],
)
def test_verifier_accuracy(
    verdicts: dict[str, bool], labels: dict[str, str], accuracy: float | None
) -> None:
    assert verifier_accuracy(verdicts, {k: _label(v) for k, v in labels.items()}) == accuracy


def test_load_labels_rejects_unknown_label_and_empty_reason(tmp_path: Path) -> None:
    path = tmp_path / "labels.json"
    for row in ({"label": "maybe", "reason": "x"}, {"label": "supported", "reason": ""}):
        path.write_text(json.dumps({"version": 1, "labels": {"q1": row}}), "utf-8")
        with pytest.raises(ConfigError, match="invalid"):
            load_labels(path)


def test_verifier_wording_names_what_is_not_unsupported(settings: Settings) -> None:
    assert "ניסוח במילים אחרות, השמטת פרטים ואמירה זהירה" in VERIFY_SYSTEM
    assert "מספר, סכום, תאריך או פרק זמן" in VERIFY_SYSTEM
    criteria = settings.graph.jev.criteria
    assert "paraphrase" in criteria.supported and "differs from the source" in criteria.unsupported
    assert VERIFY_SCHEMA["required"] == ["supported", "unsupported_claims"]


def test_criterion_5_v4a_markdown_free_per_tier(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 5: "The eval reports `markdown_free` per tier." """
    questions = load_questions(ROOT / "eval" / "questions.json")[:4]
    marked = FakeAnswerModel(canned="**תשובה** [1].")
    combos = [
        make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier()),
        make_graph_deps(fixture_store, marked, 0.3, FakeVerifier(), "qwen-openrouter"),
    ]
    outcome, reports = run_eval(questions, combos[0].ask, make_guard(priced), combos)
    assert outcome.exit_code == 0
    assert [(r.tier, r.markdown_free) for r in reports] == [
        ("haiku", 1.0),
        ("qwen-openrouter", 0.0),
    ]
    assert all(row.markdown_stripped == 2 for row in reports[1].rows)
    assert _column(format_table(reports), "markdown free") == ["1.00", "0.00"]
    retrieval = run_eval(questions, combos[0].ask, make_guard(priced), [])[1][0]
    assert retrieval.markdown_free is None
