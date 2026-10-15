from __future__ import annotations

import json
import operator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import EMPLOYMENT_QUESTION, make_graph_deps, make_guard
from zchut import cli
from zchut.answer.model import FakeAnswerModel
from zchut.answer.render import render_html
from zchut.bot.app import (
    BUDGET_LINE,
    DENIED_LINE,
    HELP_TEXT,
    BotDeps,
    create_app,
    webhook_path,
)
from zchut.bot.sessions import FirestoreSessionStore, InMemorySessionStore, Session
from zchut.bot.telegram import (
    SECRET_HEADER,
    TelegramClient,
    TelegramError,
    recording_client,
    synthetic_update,
)
from zchut.cli import app
from zchut.config import BotSettings, Prices
from zchut.graph.build import converse
from zchut.graph.verify import FakeVerifier
from zchut.index.store import InMemoryVectorStore
from zchut.run.records import REDACTED, FirestoreRunSink
from zchut.types import CallRecord, RunRecord

runner = CliRunner()
SECRET = "webhook-secret-for-tests"
CHAT = 987654321
ELIGIBLE = "מי זכאי לדמי אבטלה?"
AMOUNT = "כמה אקבל דמי אבטלה?"
NOW = datetime(2026, 10, 13, 15, 0)


class FakeSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class FakeDocument:
    def __init__(self, rows: dict[str, dict[str, Any]], doc_id: str) -> None:
        self._rows, self.id = rows, doc_id

    def get(self) -> FakeSnapshot:
        return FakeSnapshot(self.id, self._rows.get(self.id))

    def set(self, data: dict[str, Any]) -> None:
        self._rows[self.id] = dict(data)


class FakeQuery:
    def __init__(self, rows: dict[str, dict[str, Any]], where: Any) -> None:
        self._rows, self._where = rows, where

    def stream(self) -> list[FakeSnapshot]:
        test = {">=": operator.ge, "==": operator.eq}[self._where.op_string]
        f = self._where
        return [FakeSnapshot(k, v) for k, v in self._rows.items() if test(v[f.field_path], f.value)]


class FakeCollection:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows

    def document(self, doc_id: str) -> FakeDocument:
        return FakeDocument(self.rows, doc_id)

    def where(self, *, filter: Any) -> FakeQuery:  # noqa: A002
        return FakeQuery(self.rows, filter)


class FakeFirestore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self.data.setdefault(name, {}))


class Bot:
    def __init__(self, store: InMemoryVectorStore, priced: Prices, **settings: Any) -> None:
        self.db = FakeFirestore()
        self.model = FakeAnswerModel()
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.deps = BotDeps(
            make_graph_deps(store, self.model, 0.3, FakeVerifier()),
            priced,
            BotSettings(**settings),
            recording_client(lambda m, body: self.sent.append((m, body))),
            FirestoreSessionStore(self.db, timedelta(minutes=30)),
            FirestoreRunSink(self.db),
            SECRET,
            frozenset(),
            now=lambda: NOW,
        )
        self.client = TestClient(create_app(self.deps))

    def post(self, body: dict[str, object], secret: str | None = SECRET) -> httpx.Response:
        headers = {SECRET_HEADER: secret} if secret is not None else {}
        return self.client.post(webhook_path(SECRET), json=body, headers=headers)

    def say(self, text: str, chat_id: int = CHAT) -> str:
        before = len(self.sent)
        assert self.post(synthetic_update(chat_id, text)).status_code == 200
        [reply] = [b["text"] for m, b in self.sent[before:] if m == "sendMessage"]
        return str(reply)

    def methods(self) -> list[str]:
        return [m for m, _ in self.sent]

    def runs(self) -> list[dict[str, Any]]:
        return list(self.db.data.get("runs", {}).values())

    def session(self, chat_id: int = CHAT) -> dict[str, Any]:
        return self.db.data["sessions"][str(chat_id)]


def _seed_run(db: FakeFirestore, doc_id: str, started: datetime, cost: float) -> None:
    call = CallRecord(
        role="answer",
        model="claude-haiku-4-5",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        latency_ms=1,
        cost_usd=cost,
    )
    record = RunRecord(
        command="bot",
        started_at=started.isoformat(timespec="seconds"),
        dry_run=False,
        max_usd=0.08,
        calls=[call],
        total_cost_usd=cost,
    )
    db.collection("runs").document(doc_id).set(record.model_dump())


def test_criterion_1_webhook_replies_with_the_html_answer_and_checks_the_secret(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 1: "A valid update with the correct secret header gets HTTP 200 and produces exactly one `sendChatAction` and one `sendMessage` call whose text is the v4a HTML rendering of the graph's final answer (fake graph deps, stubbed Telegram transport); a wrong or missing secret gets 403 and no Telegram call." """
    bot = Bot(fixture_store, priced)
    assert bot.post(synthetic_update(CHAT, ELIGIBLE)).status_code == 200
    assert bot.methods() == ["sendChatAction", "sendMessage"]
    graph = make_graph_deps(fixture_store, FakeAnswerModel(), 0.3, FakeVerifier())
    state = converse(graph, make_guard(priced), ELIGIBLE, [], None)
    assert state.draft is not None
    action, message = (body for _, body in bot.sent)
    assert action == {"chat_id": CHAT, "action": "typing"}
    assert message == {
        "chat_id": CHAT,
        "text": render_html(state.draft),
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    assert '<a href="' in str(message["text"])

    for secret in ("wrong", None):
        assert bot.post(synthetic_update(CHAT, ELIGIBLE), secret).status_code == 403
    assert bot.client.post("/telegram/other", json={}).status_code == 404
    assert len(bot.sent) == 2 and len(bot.runs()) == 1


def test_criterion_2_clarification_is_kept_in_the_session_and_resumed(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 2: "A question that triggers clarification stores `pending_clarification` in the session and replies with the clarification text; the user's next message resumes the graph and the final reply cites sources; the session is cleared of the pending state afterwards." """
    bot = Bot(fixture_store, priced, process_inline=True)
    assert bot.say(AMOUNT) == escape(EMPLOYMENT_QUESTION, quote=False)
    assert bot.session()["pending_clarification"] == AMOUNT
    assert bot.model.calls == 0

    reply = bot.say("שכיר")
    assert "<b>מקורות:</b>" in reply and '<a href="https://www.kolzchut.org.il/' in reply
    assert bot.model.calls == 1
    session = bot.session()
    assert session["pending_clarification"] is None
    assert session["history"] == [f"{AMOUNT} (שכיר)"]
    first, second = (r["graph"] for r in bot.runs())
    assert first["path"] == ["classify"] and not first["clarified"]
    assert second["clarified"] and second["path"][-1] == "final"


def test_criterion_3_help_and_unlisted_chats_make_no_model_call(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 3: "`/start` and `/help` reply with the fixed Hebrew text and make no model call; a message from a chat id outside `TELEGRAM_ALLOWED_CHAT_IDS` gets the refusal line, no model call, and is counted in the record." """
    bot = Bot(fixture_store, priced, process_inline=True)
    help_html = escape(HELP_TEXT, quote=False)
    assert bot.say("/start") == help_html and bot.say("/help@zchut_bot") == help_html
    for words in ("דמי אבטלה", "פנסיה", "מס הכנסה", "CC BY-SA", "לא ייעוץ משפטי"):
        assert words in HELP_TEXT
    assert "sendChatAction" not in bot.methods()

    bot.deps = replace(bot.deps, allowed_chat_ids=frozenset({111}))
    bot.client = TestClient(create_app(bot.deps))
    assert bot.say(ELIGIBLE) == DENIED_LINE
    assert bot.model.calls == 0
    assert all(r["calls"] == [] for r in bot.runs())
    assert [r["bot_event"] for r in bot.runs()] == ["help", "help", "denied"]

    assert "<a href" in bot.say(ELIGIBLE, chat_id=111) and bot.model.calls == 1


def test_criterion_5_over_the_daily_budget_no_embed_or_model_call(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 5: "Over the daily budget (fixture records for today summing above the cap) the bot replies with the fixed line and makes no embed or model call; under it, it proceeds." """
    bot = Bot(fixture_store, priced, process_inline=True, max_usd_per_day=1.0)
    _seed_run(bot.db, "yesterday", NOW - timedelta(days=1), 5.0)
    _seed_run(bot.db, "morning", NOW.replace(hour=8), 0.6)
    assert bot.say(ELIGIBLE) != BUDGET_LINE
    assert bot.model.calls == 1

    _seed_run(bot.db, "noon", NOW.replace(hour=12), 0.6)
    assert bot.say(ELIGIBLE) == BUDGET_LINE
    assert bot.model.calls == 1
    budget = [r for r in bot.runs() if r["bot_event"] == "budget"]
    assert len(budget) == 1 and budget[0]["calls"] == []


def test_criterion_6_webhook_records_hold_no_chat_id_name_or_text(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    """criterion 6: "Run records written by the webhook contain the graph path, tier, cost and hashes only: no chat id, user name, message text or history (redaction test)." """
    bot = Bot(fixture_store, priced, process_inline=True)
    sender = {"id": CHAT, "first_name": "ישראלה", "username": "israela_test"}
    for text in (ELIGIBLE, AMOUNT, "שכיר"):
        message = {"message_id": 1, "chat": {"id": CHAT}, "from": sender, "text": text}
        assert bot.post({"update_id": 1, "message": message}).status_code == 200
    runs = bot.runs()
    assert len(runs) == 3
    dumped = json.dumps({"ids": list(bot.db.data["runs"]), "runs": runs}, ensure_ascii=False)
    for private in (str(CHAT), "ישראלה", "israela_test", ELIGIBLE, AMOUNT, "שכיר", SECRET):
        assert private not in dumped
    assert REDACTED not in dumped
    last = runs[-1]
    assert last["graph"]["path"][-1] == "final" and last["graph"]["tier"] == "haiku"
    assert last["total_cost_usd"] > 0 and last["question_hash"] and last["history_hash"]


def test_criterion_7_bot_simulate_dry_run_prints_html_with_links(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """criterion 7: "`zchut bot simulate "מי זכאי לדמי אבטלה?" --dry-run` prints an HTML reply with `<a href` sources and exit 0, with no network." """

    def offline(*_args: object, **_kwargs: object) -> httpx.Response:
        raise AssertionError("network used")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", offline)
    result = runner.invoke(app, ["bot", "simulate", ELIGIBLE, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "<b>מקורות:</b>" in result.output and '<a href="https://' in result.output
    [record] = (json.loads(p.read_text("utf-8")) for p in (cli_root / "runs").glob("*-bot.json"))
    assert record["bot_event"] == "answer" and ELIGIBLE not in json.dumps(record)


def test_criterion_8_v4_pytest_passes_every_criterion_quoted() -> None:
    """criterion 8: "`pytest` passes; every criterion has a test whose name quotes it." """
    from test_spec_coverage import criteria

    assert len(criteria("2026-10-13-zchut-v4-telegram")) == 8


def test_bot_serve_dry_run_builds_the_app_without_starting_a_real_server(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[Any] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda server, **kw: served.append((server, kw)))
    result = runner.invoke(app, ["bot", "serve", "--dry-run", "--port", "8099"])
    assert result.exit_code == 0, result.output
    [(server, options)] = served
    assert options == {"host": "127.0.0.1", "port": 8099}
    path = webhook_path("dry-run")
    assert path in result.output
    body = synthetic_update(CHAT, "/help")
    with TestClient(server) as client:
        assert client.post(path, json=body, headers={SECRET_HEADER: "dry-run"}).status_code == 200


@pytest.mark.parametrize("make", ["memory", "firestore"])
def test_sessions_expire_after_the_ttl(make: str) -> None:
    now = [datetime(2026, 10, 13, 12, 0, tzinfo=UTC)]
    ttl = timedelta(minutes=30)
    db = FakeFirestore()
    store = (
        InMemorySessionStore(ttl, lambda: now[0])
        if make == "memory"
        else FirestoreSessionStore(db, ttl, lambda: now[0])
    )
    store.save(5, Session(history=["א"], pending_clarification="ב"))
    now[0] += timedelta(minutes=29)
    assert store.load(5) == Session(history=["א"], pending_clarification="ב")
    now[0] += timedelta(minutes=2)
    assert store.load(5) == Session()
    if make == "firestore":
        assert db.data["sessions"]["5"]["expires_at"] == now[0] - timedelta(minutes=1)


def test_telegram_errors_never_carry_the_token() -> None:
    token = "123:secret-token"

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = TelegramClient(httpx.Client(transport=httpx.MockTransport(fail)), token)
    with pytest.raises(TelegramError) as caught:
        client.send_message(1, "x")
    assert token not in str(caught.value) and caught.value.__suppress_context__
