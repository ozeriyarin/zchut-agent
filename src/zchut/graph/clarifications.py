from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from zchut.config import ConfigError
from zchut.graph.state import Topic


class Clarification(BaseModel):
    key: str = Field(pattern=r"^[a-z_]+$")
    question_he: str = Field(min_length=1)
    applies_to_topics: list[Topic] = Field(min_length=1)


Clarifications = dict[str, Clarification]
_LIST = TypeAdapter(list[Clarification])


def load_clarifications(path: Path) -> Clarifications:
    try:
        entries = _LIST.validate_python(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise ConfigError(f"invalid {path}: {exc}") from exc
    found = {c.key: c for c in entries}
    if len(found) != len(entries):
        raise ConfigError(f"{path}: clarification keys must be unique")
    return found


def applicable(key: str | None, topic: Topic, clarifications: Clarifications) -> str | None:
    fact = clarifications.get(key or "")
    return key if fact is not None and topic in fact.applies_to_topics else None
