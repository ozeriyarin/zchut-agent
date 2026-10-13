from __future__ import annotations

import json
import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError


class ConfigError(Exception):
    pass


class ChunkSettings(BaseModel):
    max_chars: int = Field(gt=0)
    min_chars: int = Field(ge=0)


class RetrievalSettings(BaseModel):
    k: int = Field(gt=0)
    min_score: float


class ModelSettings(BaseModel):
    answer: str
    embed: str
    qwen: str
    vllm: str
    jev: str


class AnswerSettings(BaseModel):
    max_tokens: int = Field(gt=0)
    timeout_s: float = Field(gt=0)


class FetchSettings(BaseModel):
    user_agent: str
    min_interval_s: float = Field(ge=0)
    timeout_s: float = Field(gt=0)


class EmbedSettings(BaseModel):
    max_retries: int = Field(default=5, gt=0)
    max_batch_tokens: int = Field(default=8000, gt=0)
    min_interval_s: float = Field(default=0.0, ge=0)


class CrawlSettings(BaseModel):
    api_url: str
    skip_prefixes: list[str] = []
    list_batch: int = Field(default=500, gt=0, le=500)
    revisions_batch: int = Field(default=50, gt=0, le=50)
    max_query_chars: int = Field(default=3000, gt=0)
    fetch_delay_s: float = Field(gt=0)
    tokens_per_article: int = Field(gt=0)


class JevCriteria(BaseModel):
    supported: str
    unsupported: str


class JevSettings(BaseModel):
    instructions: str
    criteria: JevCriteria


class GraphSettings(BaseModel):
    classify_max_tokens: int = Field(gt=0)
    verify_max_tokens: int = Field(gt=0)
    jev: JevSettings


class PathSettings(BaseModel):
    runs_dir: Path
    raw_dir: Path
    chroma_dir: Path
    fixtures_dir: Path
    questions: Path
    offices: Path = Path("config/offices.json")
    manifest: Path = Path("data/manifest.json")
    verifier_labels: Path = Path("eval/verifier_labels.json")


class Settings(BaseModel):
    sources: list[str]
    chunk: ChunkSettings
    retrieval: RetrievalSettings
    max_usd: float = Field(ge=0)
    models: ModelSettings
    answer: AnswerSettings
    fetch: FetchSettings
    embed: EmbedSettings = EmbedSettings()
    graph: GraphSettings
    crawl: CrawlSettings
    paths: PathSettings


class ModelPrice(BaseModel):
    input: float
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


class Prices(BaseModel):
    models: dict[str, ModelPrice]

    def for_model(self, model: str) -> ModelPrice:
        if model not in self.models:
            raise ConfigError(f"no price for model {model} in config/prices.json")
        return self.models[model]

    def with_free(self, models: list[str]) -> Prices:
        return Prices(models={**self.models, **{m: ModelPrice(input=0.0) for m in models}})


class Secrets(BaseModel):
    voyage_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    vllm_url: str | None = None
    vllm_api_key: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Secrets:
        anthropic = env.get("ZCHUT_ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY")
        return cls(
            voyage_api_key=env.get("VOYAGE_API_KEY") or None,
            anthropic_api_key=anthropic or None,
            openrouter_api_key=env.get("OPENROUTER_API_KEY") or None,
            vllm_url=env.get("ZCHUT_VLLM_URL") or None,
            vllm_api_key=env.get("ZCHUT_VLLM_API_KEY") or None,
        )

    def require_voyage(self) -> str:
        if not self.voyage_api_key:
            raise ConfigError("missing environment variable VOYAGE_API_KEY")
        return self.voyage_api_key

    def require_anthropic(self) -> str:
        if not self.anthropic_api_key:
            raise ConfigError(
                "missing environment variable ZCHUT_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)"
            )
        return self.anthropic_api_key

    def require_openrouter(self) -> str:
        if not self.openrouter_api_key:
            raise ConfigError("missing environment variable OPENROUTER_API_KEY")
        return self.openrouter_api_key

    def require_vllm_url(self) -> str:
        if not self.vllm_url:
            raise ConfigError("missing environment variable ZCHUT_VLLM_URL")
        return self.vllm_url

    def values(self) -> list[str]:
        keys = [
            self.voyage_api_key,
            self.anthropic_api_key,
            self.openrouter_api_key,
            self.vllm_api_key,
        ]
        return [k for k in keys if k]


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def load_settings(root: Path) -> Settings:
    try:
        return Settings.model_validate(_read_json(root / "config" / "zchut.json"))
    except ValidationError as exc:
        raise ConfigError(f"invalid config/zchut.json: {exc}") from exc


def load_prices(root: Path) -> Prices:
    try:
        return Prices.model_validate({"models": _read_json(root / "config" / "prices.json")})
    except ValidationError as exc:
        raise ConfigError(f"invalid config/prices.json: {exc}") from exc


SECRET_NAMES = frozenset(
    {
        "VOYAGE_API_KEY",
        "ZCHUT_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "ZCHUT_VLLM_URL",
        "ZCHUT_VLLM_API_KEY",
    }
)


def load_dotenv(path: Path, env: MutableMapping[str, str]) -> int:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    ignored = 0
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.strip()
        if not sep or key.startswith("#") or not key:
            continue
        if key not in SECRET_NAMES:
            ignored += 1
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env.setdefault(key, value)
    return ignored


def load_secrets(dotenv: Path | None = None) -> Secrets:
    if dotenv is not None:
        load_dotenv(dotenv, os.environ)
    return Secrets.from_env(os.environ)
