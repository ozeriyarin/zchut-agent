from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import AVTALA, PENSIA, ROOT, TAX, make_deps, make_guard
from zchut.answer.model import FakeAnswerModel
from zchut.cli import app
from zchut.config import ConfigError, Prices, load_settings
from zchut.eval.metrics import citation_precision, hit_at_k, rate
from zchut.eval.questions import EvalQuestion, load_questions
from zchut.eval.run_eval import run_eval
from zchut.index.store import InMemoryVectorStore

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
    for label in ("hit@3:", "דיוק ציטוטים:", "שיעור סירוב:"):
        assert label in result.output
    report = json.loads(next((cli_root / "runs").glob("*-eval-report.json")).read_text("utf-8"))
    assert report["questions"] == 30 and len(report["rows"]) == 30
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
    outcome, report = run_eval(questions, make_deps(fixture_store, model, 0.3), guard, True)
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
