from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from typer.testing import CliRunner

from conftest import EMPLOYMENT_QUESTION, OFFICES, make_graph_deps, make_guard
from zchut.answer.citations import render
from zchut.answer.model import FakeAnswerModel
from zchut.answer.refusal import REFUSAL_SENTENCE
from zchut.cli import app
from zchut.config import ConfigError, Prices
from zchut.graph.build import build_graph, converse
from zchut.graph.classify import fake_classification
from zchut.graph.offices import ESCALATION_HEAD, escalation_text, load_offices
from zchut.graph.state import ConversationState
from zchut.graph.verify import FakeVerifier, uncited_sentences
from zchut.index.store import InMemoryVectorStore
from zchut.run.converse import run_conversation
from zchut.run.cost_guard import BudgetExceeded

runner = CliRunner()
ELIGIBLE = "מי זכאי לדמי אבטלה?"
AMOUNT = "כמה אקבל דמי אבטלה?"
RECIPE = "מה המתכון לחלה מתוקה?"
PASSPORT = "איך מחדשים דרכון ישראלי?"


def test_criterion_1_in_scope_path_and_out_of_scope_refusal(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 1: "The graph classifies an in-scope unemployment question as `topic: unemployment, in_scope: true` and follows the path classify → retrieve → answer → verify → final; an out-of-scope question follows classify → refuse with the v1 refusal sentence and makes no retrieval or answer call." """
    deps = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier())
    state = converse(deps, make_guard(priced), ELIGIBLE, [], None)
    assert (state.topic, state.in_scope) == ("unemployment", True)
    assert state.path == ["classify", "retrieve", "answer", "verify", "final"]
    assert state.draft is not None and state.final == render(state.draft)
    assert state.verified is True

    model = FakeAnswerModel()
    guard = make_guard(priced)
    state = converse(
        make_graph_deps(fixture_store, model, 0.3, FakeVerifier()), guard, RECIPE, [], None
    )
    assert (state.topic, state.in_scope) == ("other", False)
    assert state.path == ["classify", "refuse"] and state.final == REFUSAL_SENTENCE
    assert model.calls == 0 and state.hits == []
    assert [c.role for c in guard.recorder.record.calls] == ["classify"]

    state = converse(deps, make_guard(priced), PASSPORT, [], None)
    assert (state.topic, state.in_scope, state.office_lookup) == ("other", True, False)
    assert state.path[:2] == ["classify", "retrieve"] and state.missing_fact is None


def test_criterion_2_one_clarification_then_best_reading(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 2: "A question missing a required fact interrupts after `classify` with one Hebrew clarification; resuming with a scripted reply re-classifies once and continues; a second missing fact does not interrupt again (the graph proceeds with its best reading)." """
    deps = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier())
    graph = build_graph(deps, make_guard(priced))
    config: RunnableConfig = {"configurable": {"thread_id": "clarify-1"}}
    out = graph.invoke(ConversationState(question=AMOUNT), config)
    [pending] = out["__interrupt__"]
    assert pending.value == EMPLOYMENT_QUESTION
    assert graph.get_state(config).values["path"] == ["classify"]
    state = ConversationState.model_validate(graph.invoke(Command(resume="שכיר"), config))
    assert state.path == [
        "classify",
        "clarify",
        "condense",
        "classify",
        "retrieve",
        "answer",
        "verify",
        "final",
    ]
    assert state.clarified and state.standalone == f"{AMOUNT} (שכיר)"
    assert state.missing_fact is None

    asked: list[str] = []

    def unhelpful(question: str) -> str:
        asked.append(question)
        return "לא יודע"

    state = converse(deps, make_guard(priced), AMOUNT, [], unhelpful)
    assert asked == [EMPLOYMENT_QUESTION]
    assert state.missing_fact == "employment_status" and state.path.count("clarify") == 1
    assert state.path[:5] == ["classify", "clarify", "condense", "classify", "retrieve"]
    assert state.final is not None


def test_criterion_3_unsupported_draft_is_replaced_by_escalation(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 3: "When the verifier returns `supported: false`, the final output is the escalation text for the topic from `config/offices.json`, including the unsupported claims, and the draft answer is not shown." """
    model = FakeAnswerModel(canned="דמי אבטלה משולמים לכל החיים.")
    deps = make_graph_deps(fixture_store, model, 0.3, FakeVerifier())
    state = converse(deps, make_guard(priced), ELIGIBLE, [], None)
    office = load_offices(OFFICES)["unemployment"]
    assert state.path[-2:] == ["verify", "escalate"] and state.verified is False
    assert state.final == escalation_text(office, ["דמי אבטלה משולמים לכל החיים"])
    assert state.final.startswith(ESCALATION_HEAD)
    assert office.contact_url in state.final
    assert "לא הצלחתי לאמת:\n- דמי אבטלה משולמים לכל החיים" in state.final
    assert state.trimmed_sentences == 0


def test_criterion_6_record_holds_the_graph_and_no_user_text(
    fixture_store: InMemoryVectorStore, priced: Prices, tmp_path: Path, cli_root: Path
) -> None:
    """criterion 6: "The record stores `graph.path`, `tier`, `verified`, `escalated`, `clarified`, and every call from every node; neither the question nor any history turn appears in the record (redaction test)." """
    history, reply = ["פוטרתי מהעבודה בחודש שעבר"], "שכיר"
    deps = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier(), "vllm")
    guard = make_guard(priced)
    outcome = run_conversation(AMOUNT, history, deps, guard, lambda _q: reply)
    assert outcome.exit_code == 0
    text = guard.recorder.write(tmp_path / "runs", [AMOUNT, *history, reply]).read_text("utf-8")
    for said in (AMOUNT, *history, reply):
        assert said not in text
    record = json.loads(text)
    assert record["graph"] == {
        "path": [
            "condense",
            "classify",
            "clarify",
            "condense",
            "classify",
            "retrieve",
            "answer",
            "verify",
            "final",
        ],
        "tier": "vllm",
        "verifier": "haiku",
        "verified": True,
        "escalated": False,
        "clarified": True,
        "office_lookup": False,
        "trimmed_sentences": 0,
    }
    assert [c["role"] for c in record["calls"]] == [
        "condense",
        "classify",
        "condense",
        "classify",
        "embed",
        "answer",
        "verify",
    ]
    assert record["history_hash"] and record["question_hash"]

    state = converse(deps, make_guard(priced), AMOUNT, history, lambda _q: reply)
    assert [c.role for c in state.calls] == [c["role"] for c in record["calls"]]

    result = runner.invoke(app, ["ask", AMOUNT, "--dry-run", "--reply", reply])
    assert result.exit_code == 0, result.output
    assert result.output.startswith(EMPLOYMENT_QUESTION)
    text = next((cli_root / "runs").glob("*-ask.json")).read_text("utf-8")
    assert AMOUNT not in text and reply not in text
    assert json.loads(text)["graph"]["clarified"] is True


def test_chat_keeps_history_and_writes_one_redacted_record_per_question(cli_root: Path) -> None:
    first, second = ELIGIBLE, "ומה צריך להביא לרישום?"
    result = runner.invoke(app, ["chat", "--dry-run"], input=f"{first}\n{second}\n\n")
    assert result.exit_code == 0, result.output
    paths = sorted((cli_root / "runs").glob("*-chat*.json"))
    assert len(paths) == 2
    hashes = sorted(json.loads(p.read_text("utf-8"))["history_hash"] or "" for p in paths)
    assert hashes[0] == "" and hashes[1]
    for path in paths:
        text = path.read_text("utf-8")
        assert first not in text and second not in text


def test_whole_path_estimate_aborts_before_the_first_call(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    guard = make_guard(priced, max_usd=0.002)
    deps = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier())
    with pytest.raises(BudgetExceeded):
        converse(deps, guard, ELIGIBLE, [], None)
    assert guard.recorder.record.calls == []
    no_verify = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, None)
    assert converse(no_verify, make_guard(priced, 0.02), ELIGIBLE, [], None).final


@pytest.mark.parametrize(
    ("text", "topic", "in_scope", "missing"),
    [
        (ELIGIBLE, "unemployment", True, None),
        (AMOUNT, "unemployment", True, "employment_status"),
        (f"{AMOUNT}\nעצמאי", "unemployment", True, None),
        ("כמה המעסיק חייב להפריש לפנסיה?", "pension", True, None),
        ("תוך כמה זמן מתקבל החזר המס?", "tax", True, None),
        ("אילו מסמכים צריך להכין?", "other", True, None),
        ("כמה כסף זה דמי לידה?", "other", True, None),
        ("מה מזג האוויר מחר?", "other", False, None),
        (RECIPE, "other", False, None),
        (PASSPORT, "other", True, None),
        ("כמה עולה לחדש דרכון?", "other", True, None),
        ("איך מוציאים רישיון נהיגה לאופנוע?", "other", True, None),
        ("איך משנים כתובת בתעודת הזהות?", "other", True, None),
        ("כמה עולה כרטיס טיסה לאילת?", "other", False, None),
    ],
)
def test_fake_classification(text: str, topic: str, in_scope: bool, missing: str | None) -> None:
    result = fake_classification(text)
    assert (result.topic, result.in_scope, result.missing_fact) == (topic, in_scope, missing)


@pytest.mark.parametrize(
    ("draft", "claims"),
    [
        ("א [1]. ב [2].", []),
        ("א [1, 2]. ב בלי מקור. ג [3]!", ["ב בלי מקור"]),
        ("שיעור 6.5 אחוז [1].", []),
        ("רק טענה", ["רק טענה"]),
    ],
)
def test_uncited_sentences(draft: str, claims: list[str]) -> None:
    assert uncited_sentences(draft) == claims


def test_offices_cover_every_topic_and_reject_gaps(tmp_path: Path) -> None:
    offices = load_offices(OFFICES)
    assert set(offices) == {"unemployment", "pension", "tax", "other"}
    assert all(o.contact_url.startswith("https://") for o in offices.values())
    assert "לא הצלחתי לאמת:" not in escalation_text(offices["tax"], [])
    partial = tmp_path / "offices.json"
    partial.write_text(json.dumps({"tax": offices["tax"].model_dump()}), "utf-8")
    with pytest.raises(ConfigError, match="lacks offices"):
        load_offices(partial)
