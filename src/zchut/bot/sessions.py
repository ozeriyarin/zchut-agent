from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from google.cloud.firestore import Client as FirestoreClient
from pydantic import BaseModel

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Session(BaseModel):
    history: list[str] = []
    pending_clarification: str | None = None


class SessionStore(Protocol):
    def load(self, chat_id: int) -> Session: ...

    def save(self, chat_id: int, session: Session) -> None: ...


class InMemorySessionStore:
    def __init__(self, ttl: timedelta, clock: Clock = utc_now) -> None:
        self._ttl = ttl
        self._clock = clock
        self._rows: dict[int, tuple[Session, datetime]] = {}

    def load(self, chat_id: int) -> Session:
        session, expires_at = self._rows.get(chat_id, (Session(), self._clock()))
        return session if expires_at > self._clock() else Session()

    def save(self, chat_id: int, session: Session) -> None:
        self._rows[chat_id] = (session, self._clock() + self._ttl)


class FirestoreSessionStore:
    def __init__(
        self,
        client: FirestoreClient,
        ttl: timedelta,
        clock: Clock = utc_now,
        collection: str = "sessions",
    ) -> None:
        self._sessions = client.collection(collection)
        self._ttl = ttl
        self._clock = clock

    def load(self, chat_id: int) -> Session:
        data = self._sessions.document(str(chat_id)).get().to_dict()
        # Firestore TTL deletes within a day of expires_at, so expiry is also enforced on read.
        if not data or data["expires_at"] <= self._clock():
            return Session()
        return Session.model_validate(data)

    def save(self, chat_id: int, session: Session) -> None:
        now = self._clock()
        doc = {**session.model_dump(), "updated_at": now, "expires_at": now + self._ttl}
        self._sessions.document(str(chat_id)).set(doc)
