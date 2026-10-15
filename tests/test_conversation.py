from __future__ import annotations

import json
from dataclasses import replace
from html import escape
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import (
    AVTALA,
    CLARIFICATIONS,
    OFFICES,
    ROOT,
    TAX,
    make_graph_deps,
    make_guard,
)
from test_answer import hit
from test_bot import Bot
from test_tiers import anthropic_client
from zchut.answer.citations import display_hits, resolve, sources_block
from zchut.answer.model import FakeAnswerModel
from zchut.answer.refusal import REFUSAL_SENTENCE
from zchut.answer.render import clean_answer, render_html
from zchut.cli import app
from zchut.config import ConfigError, Prices
from zchut.eval.conversations import load_conversations, turn_outcome
from zchut.graph.build import converse
from zchut.graph.clarifications import load_clarifications
from zchut.graph.classify import FakeClassifier, classify_schema, classify_system
from zchut.graph.condense import (
    CONDENSE_SCHEMA,
    CONDENSE_SYSTEM,
    GREETING_LINE,
    NUDGE_LINE,
    FakeCondenser,
    HaikuCondenser,
    Turns,
)
from zchut.graph.offices import ESCALATION_HEAD, load_offices, office_line
from zchut.graph.state import Classification, Session, Topic
from zchut.graph.verify import FakeVerifier
from zchut.index.store import InMemoryVectorStore
from zchut.run.ask import retrieve
from zchut.run.converse import record_state, run_conversation, take_turn
from zchut.run.cost_guard import CostGuard
from zchut.run.records import question_hash
from zchut.types import Hit

runner = CliRunner()
ELIGIBLE = "מי זכאי לדמי אבטלה?"
AMOUNT = "כמה מגיע לי באבטלה?"
OFFICE = "אוקיי, איפה המשרד שלהם?"
RESOLVED = "אוקיי, איפה המשרד של שירות התעסוקה?"


def test_criterion_1_condense_resolves_follow_ups_and_keeps_standalone_questions(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 1: "With history, `condense` returns a standalone question that resolves "איפה המשרד שלהם" after an unemployment answer into a question naming the employment service, and leaves an already standalone question unchanged (fake condenser in tests, prompt text asserted)." """
    guard = make_guard(priced)
    resolved = FakeCondenser().condense(Turns([ELIGIBLE], OFFICE), guard)
    assert (resolved.standalone_question, resolved.is_question) == (RESOLVED, True)
    pension = "כמה המעסיק חייב להפריש לפנסיה?"
    kept = FakeCondenser().condense(Turns([ELIGIBLE], pension), guard)
    assert kept.standalone_question == pension and not kept.is_reply_to_clarification
    for rule in (
        "פתור כינויים והשמטות",
        "המשרד של שירות התעסוקה",
        "שלב את התשובה בשאלה הממתינה",
        "החזר אותה בדיוק כפי שהיא",
    ):
        assert rule in CONDENSE_SYSTEM

    sent: list[dict[str, object]] = []
    payload = {"standalone_question": RESOLVED, "is_reply_to_clarification": False}
    client = anthropic_client([{**payload, "is_question": True}], sent)
    turns = Turns([ELIGIBLE], OFFICE)
    assert HaikuCondenser(client, "claude-haiku-4-5", 200).condense(turns, guard) == resolved
    assert turns.prompt() == f"שאלות קודמות:\n1. {ELIGIBLE}\nהודעה אחרונה: {OFFICE}"
    assert sent[0]["system"] == CONDENSE_SYSTEM
    assert sent[0]["messages"] == [{"role": "user", "content": turns.prompt()}]
    assert sent[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": CONDENSE_SCHEMA}
    }

    deps = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier())
    assert converse(deps, make_guard(priced), ELIGIBLE, [], None).path[0] == "classify"


def test_criterion_2_replies_fold_and_non_questions_get_the_nudge(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 2: "A reply to a pending clarification is folded into the question and answered without a second clarification; a non-question with nothing pending gets the fixed nudge line, no retrieval and no answer call." """
    model, searched = FakeAnswerModel(), []
    deps = make_graph_deps(fixture_store, model, 0.3, FakeVerifier())

    def search(question: str, guard: CostGuard) -> list[Hit]:
        searched.append(question)
        return retrieve(question, deps.ask, guard)

    deps = replace(deps, retriever=search)
    guard = make_guard(priced)
    asked, session = take_turn(deps, guard, AMOUNT, Session())
    assert asked.final is None and session.pending_clarification == AMOUNT
    state, session = take_turn(deps, guard, "שכיר", session)
    assert state.path == ["condense", "classify", "retrieve", "answer", "verify", "final"]
    assert state.clarified and searched == [f"{AMOUNT} (שכיר)"] and model.calls == 1
    assert session == Session(history=[f"{AMOUNT} (שכיר)"], clarified_once=True)

    before = len(guard.recorder.record.calls)
    nudged, after = take_turn(deps, guard, "מה קשור?", session)
    assert (nudged.final, nudged.path) == (NUDGE_LINE, ["condense", "nudge"])
    assert after == session and nudged.hits == [] and len(searched) == 1 and model.calls == 1
    assert [c.role for c in guard.recorder.record.calls[before:]] == ["condense"]


class SalaryForTax(FakeClassifier):
    def classify(self, text: str, guard: CostGuard) -> Classification:
        return Classification(topic="tax", in_scope=True, missing_fact="monthly_salary")


def test_criterion_3_fixed_clarifications_at_most_once_per_session(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 3: "Clarifications are the fixed `question_he` from `config/clarifications.json`; "מי זכאי לדמי אבטלה?" and "איך נרשמים" never trigger one; "כמה מגיע לי באבטלה?" triggers at most one per session." """
    facts = load_clarifications(CLARIFICATIONS)
    assert {k: (c.question_he, c.applies_to_topics) for k, c in facts.items()} == {
        "employment_status": ("האם עבדת כשכיר/ה או כעצמאי/ת?", ["unemployment"]),
        "months_worked": ("כמה חודשים עבדת ב-18 החודשים האחרונים?", ["unemployment"]),
        "monthly_salary": ("מה היה השכר החודשי הממוצע שלך?", ["unemployment", "pension"]),
    }
    system = classify_system(facts)
    assert "אל תבקש הבהרה לעולם על שאלות זכאות (מי זכאי), איך עושים, איפה" in system
    assert all(f"{k}: {c.question_he}" in system for k, c in facts.items())

    bot = Bot(fixture_store, priced, process_inline=True)
    for question in (ELIGIBLE, "איך נרשמים כדורש עבודה בשירות התעסוקה?"):
        assert "<b>מקורות:</b>" in bot.say(question)
    assert bot.say(AMOUNT) == escape(facts["employment_status"].question_he, quote=False)
    assert "<b>מקורות:</b>" in bot.say("שכיר")
    assert "<b>מקורות:</b>" in bot.say(AMOUNT)
    events = ["answer", "answer", "clarify", "answer", "answer"]
    assert [r["bot_event"] for r in bot.runs()] == events

    deps = replace(
        make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, None), classifier=SalaryForTax()
    )
    state = converse(deps, make_guard(priced), "כמה מס אשלם?", [], None, pause=True)
    assert state.missing_fact is None and "clarify" not in state.path


def test_criterion_4_office_question_returns_the_office_entry(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 4: "An office question with a known topic returns the office entry text with `office_lookup: true`, not the refusal sentence." """
    model = FakeAnswerModel()
    deps = make_graph_deps(fixture_store, model, 0.3, FakeVerifier())
    guard = make_guard(priced)
    state = converse(deps, guard, OFFICE, [ELIGIBLE], None)
    office = load_offices(OFFICES)["unemployment"]
    assert state.final == office_line(office) and office.contact_url in state.final
    assert state.final != REFUSAL_SENTENCE and ESCALATION_HEAD not in state.final
    assert state.path == ["condense", "classify", "escalate"] and model.calls == 0
    record = guard.recorder.record
    record_state(record, deps, state)
    assert record.graph is not None and record.graph.office_lookup is True
    assert record.graph.escalated is False and record.refused is False


def test_criterion_5_citations_renumbered_in_order_of_first_appearance(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 5: "Citations in the final answer are renumbered in order of first appearance and the sources block matches; a dropped invented number does not consume a display number." """
    answer = resolve("א [3]. ב [9] [1]. ג [3, 2].", [hit(1, TAX, None), hit(2), hit(3)])
    assert answer.text == "א [1]. ב [2]. ג [1, 3]." and answer.dropped_citations == 1
    assert [(c.number, c.chunk.id) for c in answer.citations] == [
        (1, "c-3"),
        (2, "c-1"),
        (3, "c-2"),
    ]
    assert sources_block(answer.citations).splitlines()[1:3] == [
        f"[1, 3] כותרת 3 — {AVTALA} (עודכן: 12.8.2026)",
        f"[2] כותרת 1 — {TAX}",
    ]

    model = FakeAnswerModel(canned="ראשון [3]. שני [7] [1].")
    deps = make_graph_deps(fixture_store, model, 0.3, FakeVerifier())
    state = converse(deps, make_guard(priced), ELIGIBLE, [], None)
    assert state.draft is not None and state.final is not None
    assert state.final.startswith("ראשון [1]. שני [2].\n\nמקורות:\n[1, 2] ")
    assert display_hits(state.draft, state.hits)[:2] == [state.hits[2], state.hits[0]]


def test_criterion_6_conversation_eval_dry_run_scores_every_turn(cli_root: Path) -> None:
    """criterion 6: "`zchut eval --conversations --dry-run` runs the five live conversations with the expected outcomes per turn and reports turn accuracy; the suite asserts 100% on the fakes." """
    facts = load_clarifications(CLARIFICATIONS)
    found = load_conversations(ROOT / "eval" / "conversations.json", facts)
    assert len(found) == 6
    expects = {t.expect for c in found for t in c.turns}
    assert expects == {"answer", "clarify:employment_status", "nudge", "office"}
    result = runner.invoke(app, ["eval", "--conversations", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "דיוק תורות: 1.00 (13/13)" in result.output
    report_path = next((cli_root / "runs").glob("*-conversations-report.json"))
    report = json.loads(report_path.read_text("utf-8"))
    assert (report["conversations"], report["turns"], report["turn_accuracy"]) == (6, 13, 1.0)
    assert all(r["correct"] and r["outcome"] == r["expect"] for r in report["rows"])

    bad = cli_root / "bad.json"
    bad.write_text(json.dumps([{"id": "x", "turns": [{"message": "א", "expect": "clarify:age"}]}]))
    with pytest.raises(ConfigError, match="unknown clarification keys"):
        load_conversations(bad, facts)


def test_criterion_7_records_hold_the_standalone_hash_event_and_office_flag(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 7: "The record stores the standalone question as a hash only, never its text, together with `bot_event` and `office_lookup` (redaction test)." """
    bot = Bot(fixture_store, priced, process_inline=True)
    for text in (ELIGIBLE, OFFICE, "מה קשור?"):
        bot.say(text)
    runs = bot.runs()
    assert [r["bot_event"] for r in runs] == ["answer", "answer", "nudge"]
    assert [r["graph"]["office_lookup"] for r in runs] == [False, True, False]
    assert runs[1]["question_hash"] == question_hash(RESOLVED)
    dumped = json.dumps(runs, ensure_ascii=False)
    for private in (ELIGIBLE, OFFICE, RESOLVED, "שירות התעסוקה", "מה קשור"):
        assert private not in dumped


def test_criterion_8_v4b_pytest_passes_every_criterion_quoted() -> None:
    """criterion 8: "`pytest` passes; every criterion has a test whose name quotes it." """
    from test_spec_coverage import criteria

    assert len(criteria("2026-10-15-zchut-v4b-conversation-quality")) == 11


def test_criterion_9_greeting_on_the_first_turn_gets_the_welcome_line(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 9: "A greeting or reaction on the first turn gets the fixed welcome line with no retrieval or answer call." """
    facts = load_clarifications(CLARIFICATIONS)
    assert classify_schema(facts)["required"] == [
        "topic",
        "in_scope",
        "office_lookup",
        "missing_fact",
        "intent",
    ]
    assert "intent: greeting לברכה בלבד" in classify_system(facts)
    bot = Bot(fixture_store, priced, process_inline=True)
    assert bot.say("היי") == escape(GREETING_LINE, quote=False)
    assert bot.say("תודה!") == escape(NUDGE_LINE, quote=False)
    assert "<b>מקורות:</b>" in bot.say(ELIGIBLE)
    runs = bot.runs()
    assert [r["bot_event"] for r in runs] == ["nudge", "nudge", "answer"]
    assert [[c["role"] for c in r["calls"]] for r in runs[:2]] == [["classify"], ["classify"]]
    assert bot.model.calls == 1 and bot.session()["history"] == [ELIGIBLE]

    deps = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, None)
    asked, session = take_turn(deps, make_guard(priced), AMOUNT, Session())
    state, _ = take_turn(deps, make_guard(priced), "שלום", session)
    assert "nudge" not in state.path and state.clarified


class OfficeMissed(FakeClassifier):
    def __init__(self, topic: Topic, flagged: bool) -> None:
        super().__init__()
        self.topic, self.flagged = topic, flagged

    def classify(self, text: str, guard: CostGuard) -> Classification:
        return Classification(topic=self.topic, in_scope=True, office_lookup=self.flagged)


@pytest.mark.parametrize(
    ("topic", "flagged", "question"),
    [
        ("unemployment", False, "איפה נמצא הסניף של שירות התעסוקה?"),
        ("other", True, "איך פונים לגבי דמי אבטלה?"),
    ],
    ids=["condensed-question-asks", "classifier-flag-unknown-topic"],
)
def test_criterion_10_office_question_from_corpus_ends_with_the_office(
    fixture_store: InMemoryVectorStore, priced: Prices, topic: Topic, flagged: bool, question: str
) -> None:
    """criterion 10: "An office question answered from the corpus still ends with the office's name and contact page." """
    base = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier())
    deps = replace(base, classifier=OfficeMissed(topic, flagged))
    state = converse(deps, make_guard(priced), question, [], None)
    assert state.path == ["classify", "retrieve", "answer", "verify", "final"]
    line = office_line(load_offices(OFFICES)[topic])
    assert state.final is not None and state.draft is not None
    assert state.final.split("\n\nמקורות:\n")[0].endswith(f"\n\n{line}")
    assert render_html(state.draft).split("\n\n<b>מקורות:</b>")[0].endswith(escape(line))
    assert turn_outcome(state) == "office"

    plain = converse(base, make_guard(priced), ELIGIBLE, [], None)
    assert plain.final is not None and "כדאי לפנות ל" not in plain.final
    assert turn_outcome(plain) == "answer"


@pytest.mark.parametrize(
    ("raw", "clean", "scrubbed"),
    [
        ("דמי 실업אבטלה [1].", "דמי אבטלה [1].", 1),
        ("תשובה 中文 ו-Привет ok [2].", "תשובה  ו- ok [2].", 2),
        ("שָׁלוֹם café 15% ₪200 ✓ [1].", "שָׁלוֹם café 15% ₪200 ✓ [1].", 0),
    ],
    ids=["hangul-mid-word", "cjk-and-cyrillic", "hebrew-latin-symbols-kept"],
)
def test_criterion_11_foreign_script_scrubbed_and_counted(
    fixture_store: InMemoryVectorStore, priced: Prices, raw: str, clean: str, scrubbed: int
) -> None:
    """criterion 11: "Characters outside Hebrew, Latin, digits and punctuation are removed from the answer and counted in the record." """
    assert clean_answer(raw) == (clean, 0, scrubbed)
    model = FakeAnswerModel(canned=f"**{raw}**")
    deps = make_graph_deps(fixture_store, model, 0.3, FakeVerifier())
    guard = make_guard(priced)
    assert run_conversation(ELIGIBLE, [], deps, guard, None).exit_code == 0
    record = guard.recorder.record
    assert (record.scrubbed, record.markdown_stripped) == (scrubbed, 2)
