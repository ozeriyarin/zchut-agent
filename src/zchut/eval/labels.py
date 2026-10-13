from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from zchut.config import ConfigError
from zchut.eval.metrics import rate


class VerifierLabel(BaseModel):
    label: Literal["supported", "unsupported"]
    reason: str = Field(min_length=1)


class VerifierLabels(BaseModel):
    version: Literal[1]
    labels: dict[str, VerifierLabel]


def load_labels(path: Path) -> dict[str, VerifierLabel]:
    if not path.exists():
        return {}
    try:
        return VerifierLabels.model_validate_json(path.read_text(encoding="utf-8")).labels
    except (OSError, ValidationError) as exc:
        raise ConfigError(f"invalid {path}: {exc}") from exc


def verifier_accuracy(
    verdicts: Mapping[str, bool], labels: Mapping[str, VerifierLabel]
) -> float | None:
    judged = [
        ok == (labels[qid].label == "supported") for qid, ok in verdicts.items() if qid in labels
    ]
    return rate(judged) if judged else None
