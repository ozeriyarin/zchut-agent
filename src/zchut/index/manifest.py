from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError


class ManifestError(Exception):
    pass


class ManifestEntry(BaseModel):
    revid: int
    last_updated: str | None
    fetched_at: str
    chunks: int
    status: Literal["ok", "error"]
    error: str | None = None


class Manifest(BaseModel):
    version: Literal[1] = 1
    articles: dict[str, ManifestEntry] = {}


def load_manifest(path: Path) -> Manifest:
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Manifest()
    except (OSError, ValidationError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc


def save_manifest(path: Path, manifest: Manifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(manifest.model_dump_json(indent=1, exclude_none=True), encoding="utf-8")
    os.replace(temp, path)
