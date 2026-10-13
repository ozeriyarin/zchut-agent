from __future__ import annotations

import json
from collections.abc import Callable

import httpx
from pydantic import BaseModel

TELEGRAM_API = "https://api.telegram.org"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
SendLog = Callable[[str, dict[str, object]], None]


class TelegramError(Exception):
    pass


class Chat(BaseModel):
    id: int


class Message(BaseModel):
    message_id: int
    chat: Chat
    text: str | None = None


class Update(BaseModel):
    update_id: int
    message: Message | None = None


def synthetic_update(chat_id: int, text: str) -> dict[str, object]:
    chat = {"id": chat_id, "type": "private"}
    return {"update_id": 1, "message": {"message_id": 1, "chat": chat, "text": text}}


class TelegramClient:
    def __init__(self, http: httpx.Client, token: str) -> None:
        self._http = http
        self._token = token

    def send_chat_action(self, chat_id: int) -> None:
        self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    def send_message(self, chat_id: int, html: str) -> None:
        body = {
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        self._call("sendMessage", body)

    def _call(self, method: str, body: dict[str, object]) -> None:
        # The URL holds the bot token, so errors carry only the method and the status.
        try:
            response = self._http.post(f"{TELEGRAM_API}/bot{self._token}/{method}", json=body)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise TelegramError(f"{method} answered {response.status_code}")


def recording_client(sent: SendLog) -> TelegramClient:
    def answer(request: httpx.Request) -> httpx.Response:
        sent(request.url.path.rsplit("/", 1)[-1], json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    return TelegramClient(httpx.Client(transport=httpx.MockTransport(answer)), "dry-run")
