from __future__ import annotations

import json
from pathlib import Path

import anthropic
import httpx
import pytest
from typer.testing import CliRunner

from conftest import AVTALA, TAX, make_deps, make_graph_deps, make_guard
from zchut.answer.citations import ATTRIBUTION_LINE, render, resolve, sources_block
from zchut.answer.model import FakeAnswerModel, HaikuAnswerModel
from zchut.answer.prompt import SYSTEM_PROMPT, build_prompt, format_date
from zchut.answer.refusal import REFUSAL_SENTENCE, should_refuse
from zchut.answer.render import clean_answer, render_html
from zchut.cli import app
from zchut.config import Prices
from zchut.graph.verify import FakeVerifier
from zchut.index.store import InMemoryVectorStore
from zchut.run.ask import run_ask
from zchut.run.converse import run_conversation
from zchut.types import Chunk, Hit

runner = CliRunner()
REGISTER = "מה צריך להביא לרישום בלשכת התעסוקה?"
REGISTER_DRY_RUN = (
    "תעודת זהות עם ספח [1]. כדי לקבל דמי אבטלה יש להירשם כדורש עבודה בלשכת שירות התעסוקה כבר "
    "ביום הראשון לאבטלה, או להירשם באתר שירות התעסוקה ולהגיע ללשכה במועד שנקבע [2].\n\n"
    "מקורות:\n"
    f"[1, 2] דמי אבטלה — {AVTALA} (עודכן: 12.8.2026)\n"
    f"{ATTRIBUTION_LINE}\n"
)


def hit(n: int, url: str = AVTALA, updated: str | None = "2026-08-12", score: float = 0.5) -> Hit:
    license_id = "gov.il" if url == TAX else "CC-BY-SA-3.0"
    chunk = Chunk(
        f"c-{n}",
        url,
        f"כותרת {n}",
        f"כותרת {n} > פרק",
        f"טקסט {n}",
        "2026-10-13",
        license_id,
        updated,
    )
    return Hit(chunk, score)


def test_criterion_5_answer_with_citations_and_sources(
    fixture_store: InMemoryVectorStore, priced: Prices, cli_root: Path
) -> None:
    """criterion 5: "`zchut ask` prints a Hebrew answer whose citations `[1]`, `[2]` map to the retrieved chunks, followed by a `מקורות` block with one line per cited source holding its URL and its last-updated date when present; a citation number the model invents is dropped from the answer and counted in the record." """
    model = FakeAnswerModel(canned="צריך להביא תעודת זהות [1] ופרטי בנק [2]. כך כתוב גם במקור [9].")
    guard = make_guard(priced)
    outcome = run_ask(
        "מה צריך להביא לרישום בלשכת התעסוקה?", make_deps(fixture_store, model, 0.3), guard
    )
    assert outcome.exit_code == 0
    answer, sources = outcome.output.split("\n\nמקורות:\n")
    assert answer == "צריך להביא תעודת זהות [1] ופרטי בנק [2]. כך כתוב גם במקור."
    assert sources.splitlines() == [
        f"[1, 2] דמי אבטלה — {AVTALA} (עודכן: 12.8.2026)",
        ATTRIBUTION_LINE,
    ]
    record = guard.recorder.record
    assert record.dropped_citations == 1 and record.refused is False
    assert len(record.retrieved) == 5

    result = runner.invoke(app, ["ask", "מה צריך להביא לרישום בלשכת התעסוקה?", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "[1]" in result.output and "מקורות:" in result.output
    assert f"[1, 2] דמי אבטלה — {AVTALA} (עודכן: 12.8.2026)" in result.output


def test_criterion_6_refusal_without_model_call(
    fixture_store: InMemoryVectorStore, priced: Prices, cli_root: Path
) -> None:
    """criterion 6: "When the best retrieval score is under `retrieval.min_score`, the output is exactly the fixed refusal sentence, no answer-model call is made, and the record marks `refused: true`." """
    model = FakeAnswerModel()
    guard = make_guard(priced)
    outcome = run_ask(
        "מה צריך להביא לרישום בלשכת התעסוקה?", make_deps(fixture_store, model, 0.99), guard
    )
    assert outcome.output == REFUSAL_SENTENCE
    assert model.calls == 0
    assert guard.recorder.record.refused is True
    assert [c.role for c in guard.recorder.record.calls] == ["embed"]

    result = runner.invoke(app, ["ask", "מה המתכון לחלה מתוקה?", "--dry-run"])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == REFUSAL_SENTENCE
    record = json.loads(next((cli_root / "runs").glob("*-ask.json")).read_text("utf-8"))
    assert record["refused"] is True
    assert [c["role"] for c in record["calls"]] == ["classify"]


@pytest.mark.parametrize(
    ("text", "n_hits", "expected_text", "cited", "dropped"),
    [
        ("א [1] ב [2].", 2, "א [1] ב [2].", [1, 2], 0),
        ("א [2] ב [1] ג [2].", 2, "א [2] ב [1] ג [2].", [2, 1], 0),
        ("א [1, 3] ב.", 2, "א [1] ב.", [1], 1),
        ("א [1,2].", 2, "א [1, 2].", [1, 2], 0),
        ("א [7].", 2, "א.", [], 1),
        ("א [0] ב [3] [4].", 3, "א ב [3].", [3], 2),
        ("ללא ציטוטים.", 2, "ללא ציטוטים.", [], 0),
        ("שנה [2026] ב.", 2, "שנה ב.", [], 1),
    ],
    ids=["simple", "order", "group-drop", "group-keep", "only-invented", "zero", "none", "year"],
)
def test_resolve_citations(
    text: str, n_hits: int, expected_text: str, cited: list[int], dropped: int
) -> None:
    answer = resolve(text, [hit(n) for n in range(1, n_hits + 1)])
    assert answer.text == expected_text
    assert [c.number for c in answer.citations] == cited
    assert answer.dropped_citations == dropped


def test_sources_block_dates_and_attribution() -> None:
    answer = resolve("א [3] ב [1] ג [2].", [hit(1, TAX, None), hit(2), hit(3)])
    assert sources_block(answer.citations).splitlines() == [
        "מקורות:",
        f"[1] כותרת 1 — {TAX}",
        f"[2, 3] כותרת 2 — {AVTALA} (עודכן: 12.8.2026)",
        ATTRIBUTION_LINE,
    ]
    gov_only = resolve("א [1].", [hit(1, TAX, None)])
    assert ATTRIBUTION_LINE not in render(gov_only)
    assert render(resolve("בלי מקורות.", [hit(1)])) == "בלי מקורות."


@pytest.mark.parametrize(
    ("scores", "min_score", "refuse"),
    [([], 0.3, True), ([0.29, 0.1], 0.3, True), ([0.1, 0.3], 0.3, False), ([0.9], 0.3, False)],
)
def test_should_refuse(scores: list[float], min_score: float, refuse: bool) -> None:
    assert should_refuse([hit(1, score=s) for s in scores], min_score) is refuse


def test_prompt_numbers_chunks_with_url_and_date() -> None:
    prompt = build_prompt("שאלה?", [hit(1), hit(2, TAX, None)])
    assert prompt.system == SYSTEM_PROMPT
    assert f"[1] כותרת 1 > פרק\nכתובת: {AVTALA}\nעודכן: 12.8.2026\nטקסט 1" in prompt.user
    assert f"[2] כותרת 2 > פרק\nכתובת: {TAX}\nטקסט 2" in prompt.user
    assert prompt.user.endswith("שאלה: שאלה?")
    assert format_date("2026-01-05") == "5.1.2026"


def test_haiku_model_calls_messages_api_with_stubbed_transport() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "תשובה [1]."}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 9,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 0,
                },
            },
        )

    client = anthropic.Anthropic(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    reply = HaikuAnswerModel(client, max_tokens=300).answer(build_prompt("שאלה?", [hit(1)]))
    assert reply.text == "תשובה [1]."
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (120, 9)
    assert reply.usage.cache_read_tokens == 4
    body = sent[0]
    assert (body["model"], body["max_tokens"], body["system"]) == (
        "claude-haiku-4-5",
        300,
        SYSTEM_PROMPT,
    )


@pytest.mark.parametrize(
    ("raw", "clean", "removed"),
    [
        ("**חשוב:** צריך תעודת זהות [1].", "חשוב: צריך תעודת זהות [1].", 2),
        ("## כותרת\nטקסט [1].", "כותרת\nטקסט [1].", 1),
        ("- א [1]\n  * ב [2]\n• ג [1, 2]", "א [1]\nב [2]\nג [1, 2]", 3),
        ("א [1].\n\n\n  \nב [2].\n\n", "א [1].\n\nב [2].", 0),
        ("ירידה של -5 [1] ו*כוכבית* ו-#תג.", "ירידה של -5 [1] ו*כוכבית* ו-#תג.", 0),
    ],
    ids=["bold", "heading", "bullets", "blank-lines", "inline-markers-kept"],
)
def test_clean_answer(raw: str, clean: str, removed: int) -> None:
    assert clean_answer(raw) == (clean, removed)


def test_criterion_1_v4a_clean_answer_counted_in_record(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 1: "`clean_answer` removes `**`, leading `#`/`-`/`*` markers and collapses blank lines, leaves `[n]` citations intact, and the record counts what it removed." """
    canned = "## תשובה\n\n\n**צריך** תעודת זהות [1].\n- ופרטי בנק [2]."
    guard = make_guard(priced)
    outcome = run_ask(REGISTER, make_deps(fixture_store, FakeAnswerModel(canned), 0.3), guard)
    assert outcome.output.split("\n\nמקורות:\n")[0] == (
        "תשובה\n\nצריך תעודת זהות [1].\nופרטי בנק [2]."
    )
    assert guard.recorder.record.markdown_stripped == 4

    guard = make_guard(priced)
    deps = make_graph_deps(fixture_store, FakeAnswerModel(canned), 0.3, FakeVerifier())
    assert run_conversation(REGISTER, [], deps, guard, None).exit_code == 0
    assert guard.recorder.record.markdown_stripped == 4


def test_criterion_2_v4a_render_html_and_unchanged_cli_text(cli_root: Path) -> None:
    """criterion 2: "`render_html` escapes `<`, `>`, `&` in the text, renders each source as an `<a href>` with its date, and the plain-text CLI output is unchanged for the fixture question." """
    answer = resolve("פחות מ<5 & יותר מ>2 [1] ועוד [2].", [hit(1), hit(2, TAX, None)])
    assert render_html(answer).splitlines() == [
        "פחות מ&lt;5 &amp; יותר מ&gt;2 [1] ועוד [2].",
        "",
        "<b>מקורות:</b>",
        f'[1] <a href="{AVTALA}">כותרת 1</a> (עודכן: 12.8.2026)',
        f'[2] <a href="{TAX}">כותרת 2</a>',
        ATTRIBUTION_LINE,
    ]
    assert render(answer).startswith("פחות מ<5 & יותר מ>2 [1] ועוד [2].\n\nמקורות:\n")
    assert render_html(resolve("א < ב.", [hit(1)])) == "א &lt; ב."

    result = runner.invoke(app, ["ask", REGISTER, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert result.output.split("רשומת ריצה:")[0] == REGISTER_DRY_RUN


def test_criterion_3_v4a_prompt_rules_and_markdown_free_dry_run(cli_root: Path) -> None:
    """criterion 3: "The answer prompt contains the plain-text, 6-line and "say what is missing" rules, and the fake-model dry run output has no Markdown markers." """
    for rule in (
        "כתוב טקסט רגיל בלבד, בלי Markdown",
        "כתוב לכל היותר 6 שורות",
        "אם הקטעים אינם מכילים את הפרט שנשאל, אמור זאת במשפט אחד ואז כתוב מה הם כן אומרים",
    ):
        assert rule in SYSTEM_PROMPT
    assert "ייעוץ משפטי" not in SYSTEM_PROMPT
    result = runner.invoke(app, ["ask", REGISTER, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert clean_answer(result.output)[1] == 0
    record = json.loads(next((cli_root / "runs").glob("*-ask.json")).read_text("utf-8"))
    assert record["markdown_stripped"] == 0
