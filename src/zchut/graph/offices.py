from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import get_args

from pydantic import BaseModel, TypeAdapter, ValidationError

from zchut.config import ConfigError
from zchut.graph.state import Topic

ESCALATION_HEAD = "לא הצלחתי לאמת את התשובה מול המקורות, ולכן היא לא מוצגת."


class Office(BaseModel):
    name: str
    handles: str
    contact_url: str


_OFFICES = TypeAdapter(dict[Topic, Office])


def load_offices(path: Path) -> dict[Topic, Office]:
    try:
        offices = _OFFICES.validate_python(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise ConfigError(f"invalid {path}: {exc}") from exc
    missing = set(get_args(Topic)) - set(offices)
    if missing:
        raise ConfigError(f"{path} lacks offices for {', '.join(sorted(missing))}")
    return offices


def office_line(office: Office) -> str:
    return f"כדאי לפנות ל{office.name} ({office.handles}): {office.contact_url}"


def escalation_text(office: Office, claims: Sequence[str]) -> str:
    lines = [ESCALATION_HEAD, office_line(office)]
    if claims:
        lines += ["לא הצלחתי לאמת:", *(f"- {c}" for c in claims)]
    return "\n".join(lines)
