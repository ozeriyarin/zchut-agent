from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from zchut.types import CallRecord, RunRecord, Usage

REDACTED = "[redacted]"


def question_hash(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class RunRecorder:
    def __init__(self, command: str, dry_run: bool, max_usd: float) -> None:
        self.started = datetime.now()
        self.record = RunRecord(
            command=command,
            started_at=self.started.isoformat(timespec="seconds"),
            dry_run=dry_run,
            max_usd=max_usd,
        )

    def add_call(self, role: str, model: str, usage: Usage, latency_ms: int, cost: float) -> None:
        self.record.calls.append(
            CallRecord(
                role=role,
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                latency_ms=latency_ms,
                cost_usd=cost,
            )
        )
        self.record.total_cost_usd = sum(c.cost_usd for c in self.record.calls)

    def write(self, runs_dir: Path, secrets: Sequence[str]) -> Path:
        self.record.errors = [redact(e, secrets) for e in self.record.errors]
        runs_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.started:%Y-%m-%d-%H%M%S}-{self.record.command}"
        path = runs_dir / f"{stem}.json"
        n = 1
        while path.exists():
            n += 1
            path = runs_dir / f"{stem}-{n}.json"
        text = redact(self.record.model_dump_json(indent=2), secrets)
        path.write_text(text, encoding="utf-8")
        return path
