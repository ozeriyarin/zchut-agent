from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from google.cloud.firestore import Client as FirestoreClient
from google.cloud.firestore import FieldFilter
from pydantic import ValidationError

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
                cost_estimated=usage.estimated,
            )
        )
        self.record.total_cost_usd = sum(c.cost_usd for c in self.record.calls)

    @property
    def stem(self) -> str:
        return f"{self.started:%Y-%m-%d-%H%M%S}-{self.record.command}"

    def redacted_json(self, secrets: Sequence[str]) -> str:
        self.record.errors = [redact(e, secrets) for e in self.record.errors]
        return redact(self.record.model_dump_json(indent=2), secrets)

    def write(self, runs_dir: Path, secrets: Sequence[str]) -> Path:
        text = self.redacted_json(secrets)
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{self.stem}.json"
        n = 1
        while path.exists():
            n += 1
            path = runs_dir / f"{self.stem}-{n}.json"
        path.write_text(text, encoding="utf-8")
        return path


class RunRecordSink(Protocol):
    def write(self, recorder: RunRecorder, secrets: Sequence[str]) -> str: ...

    def read(self, since: datetime) -> tuple[list[RunRecord], list[str]]: ...


class LocalRunSink:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    def write(self, recorder: RunRecorder, secrets: Sequence[str]) -> str:
        return str(recorder.write(self.runs_dir, secrets))

    def read(self, since: datetime) -> tuple[list[RunRecord], list[str]]:
        records: list[RunRecord] = []
        skipped: list[str] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            if path.stem.endswith("-report"):
                continue
            try:
                record = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError):
                skipped.append(path.name)
                continue
            if datetime.fromisoformat(record.started_at) >= since:
                records.append(record)
        return records, skipped


class FirestoreRunSink:
    def __init__(self, client: FirestoreClient, collection: str = "runs") -> None:
        self._runs = client.collection(collection)

    def write(self, recorder: RunRecorder, secrets: Sequence[str]) -> str:
        doc_id = f"{recorder.stem}-{uuid4().hex[:8]}"
        self._runs.document(doc_id).set(json.loads(recorder.redacted_json(secrets)))
        return f"firestore:runs/{doc_id}"

    def read(self, since: datetime) -> tuple[list[RunRecord], list[str]]:
        start = since.isoformat(timespec="seconds")
        records: list[RunRecord] = []
        skipped: list[str] = []
        for doc in self._runs.where(filter=FieldFilter("started_at", ">=", start)).stream():
            try:
                records.append(RunRecord.model_validate(doc.to_dict()))
            except ValidationError:
                skipped.append(doc.id)
        return records, skipped
