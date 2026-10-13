from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html import escape

from fastapi import BackgroundTasks, FastAPI, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from zchut.answer.citations import ATTRIBUTION_LINE
from zchut.answer.render import render_html
from zchut.bot.sessions import Session, SessionStore
from zchut.bot.telegram import SECRET_HEADER, Message, TelegramClient, Update
from zchut.config import BotSettings, Prices
from zchut.graph.build import GraphDeps, converse
from zchut.graph.state import HISTORY_CAP, ConversationState
from zchut.run.converse import record_state
from zchut.run.cost_guard import CostGuard, guarded
from zchut.run.records import RunRecorder, RunRecordSink

HELP_TEXT = "\n".join(
    [
        "שלום! אני עונה על שאלות על זכויות מול הרשויות בישראל, לפי עמודי כל-זכות ו-gov.il.",
        "הנושאים שאני מכסה: דמי אבטלה, פנסיה וחיסכון לשכירים, והחזרי מס הכנסה.",
        "כתבו שאלה במשפט אחד. אם חסר לי פרט, אשאל שאלת הבהרה אחת.",
        "כל תשובה מגיעה עם קישורים למקורות.",
        "המידע כאן הוא מידע כללי ולא ייעוץ משפטי; להחלטה בעניין שלכם פנו לגוף המטפל.",
        "השיחה נשמרת לזמן קצר בלבד ואז נמחקת.",
        ATTRIBUTION_LINE,
    ]
)
DENIED_LINE = "הבוט הזה פתוח כרגע רק למשתמשים מורשים."
BUDGET_LINE = "התקציב של היום נוצל. אפשר לנסות שוב מחר."
ERROR_LINE = "משהו השתבש ולא הצלחתי לענות. אפשר לנסות שוב בעוד רגע."
COMMANDS = frozenset({"/start", "/help"})
log = logging.getLogger("zchut.bot")


@dataclass(frozen=True)
class BotDeps:
    graph: GraphDeps
    prices: Prices
    settings: BotSettings
    telegram: TelegramClient
    sessions: SessionStore
    sink: RunRecordSink
    webhook_secret: str
    allowed_chat_ids: frozenset[int]
    secrets: tuple[str, ...] = ()
    dry_run: bool = False
    now: Callable[[], datetime] = datetime.now


def webhook_path(secret: str) -> str:
    return f"/telegram/{hashlib.sha256(secret.encode('utf-8')).hexdigest()[:16]}"


def spent_today(deps: BotDeps) -> float:
    records, _ = deps.sink.read(deps.now().replace(hour=0, minute=0, second=0, microsecond=0))
    return sum(r.total_cost_usd for r in records)


def _answer(deps: BotDeps, chat_id: int, text: str, recorder: RunRecorder, said: list[str]) -> str:
    record = recorder.record
    if deps.allowed_chat_ids and chat_id not in deps.allowed_chat_ids:
        record.bot_event = "denied"
        return DENIED_LINE
    if text.split()[0].split("@")[0] in COMMANDS:
        record.bot_event = "help"
        return escape(HELP_TEXT, quote=False)
    deps.telegram.send_chat_action(chat_id)
    if spent_today(deps) >= deps.settings.max_usd_per_day:
        record.bot_event = "budget"
        return BUDGET_LINE
    session = deps.sessions.load(chat_id)
    pending = session.pending_clarification
    question, history = (pending, [*session.history, text]) if pending else (text, session.history)
    said += [question, *history]
    guard = CostGuard(deps.settings.max_usd_per_message, deps.prices, recorder)
    states: list[ConversationState] = []

    def action() -> str:
        state = converse(
            deps.graph, guard, question, history, None, pause=not pending, resumed=bool(pending)
        )
        record_state(record, deps.graph, state)
        states.append(state)
        return ""

    if guarded(recorder, action).exit_code != 0:
        record.bot_event = "error"
        return ERROR_LINE
    [state] = states
    if state.final is None:
        deps.sessions.save(
            chat_id, Session(history=session.history, pending_clarification=question)
        )
        record.bot_event = "clarify"
        return escape(state.missing_fact or "", quote=False)
    turn = [question, text] if pending else [question]
    deps.sessions.save(chat_id, Session(history=[*session.history, *turn][-HISTORY_CAP:]))
    record.bot_event = "answer"
    if state.draft is not None and "final" in state.path:
        return render_html(state.draft)
    return escape(state.final, quote=False)


def handle(deps: BotDeps, message: Message) -> None:
    text, chat_id = (message.text or "").strip(), message.chat.id
    if not text:
        return
    recorder = RunRecorder("bot", deps.dry_run, deps.settings.max_usd_per_message)
    said = [text]
    try:
        reply = _answer(deps, chat_id, text, recorder, said)
    except Exception as exc:
        recorder.record.bot_event = "error"
        recorder.record.errors.append(type(exc).__name__)
        deps.telegram.send_message(chat_id, ERROR_LINE)
        raise
    finally:
        deps.sink.write(recorder, [*deps.secrets, *said])
    deps.telegram.send_message(chat_id, reply)


def process(deps: BotDeps, update: Update) -> None:
    if update.message is None:
        return
    try:
        handle(deps, update.message)
    except Exception as exc:
        log.error("update %s failed: %s", update.update_id, type(exc).__name__)


def create_app(deps: BotDeps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(webhook_path(deps.webhook_secret))
    async def telegram(request: Request, background: BackgroundTasks) -> Response:
        given = request.headers.get(SECRET_HEADER, "").encode("utf-8")
        if not hmac.compare_digest(given, deps.webhook_secret.encode("utf-8")):
            return Response(status_code=403)
        try:
            update = Update.model_validate_json(await request.body())
        except ValidationError:
            # Telegram redelivers on any non-2xx, so a malformed update is dropped with 200.
            return Response(status_code=200)
        if deps.settings.process_inline:
            await run_in_threadpool(process, deps, update)
        else:
            background.add_task(process, deps, update)
        return Response(status_code=200)

    return app


def __getattr__(name: str) -> FastAPI:
    # Built lazily so `uvicorn zchut.bot.app:app` wires production deps and tests import cleanly.
    if name != "app":
        raise AttributeError(name)
    from zchut.cli import production_app

    return production_app()
