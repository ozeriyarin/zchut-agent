from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel


@dataclass(frozen=True)
class FetchedPage:
    url: str
    html: str
    fetched_at: date
    last_updated: date | None = None


@dataclass(frozen=True)
class Section:
    headings: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    license: str
    fetched_at: date
    last_updated: date | None
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class Chunk:
    id: str
    url: str
    title: str
    section: str
    text: str
    fetched_at: str
    license: str
    last_updated: str | None


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    usage: Usage


@dataclass(frozen=True)
class ModelReply:
    text: str
    usage: Usage


@dataclass(frozen=True)
class Citation:
    number: int
    chunk: Chunk


@dataclass(frozen=True)
class Answer:
    text: str
    citations: tuple[Citation, ...]
    dropped_citations: int = 0
    refused: bool = False
    markdown_stripped: int = 0
    scrubbed: int = 0


@dataclass(frozen=True)
class IngestReport:
    pages: int
    chunks: int
    errors: tuple[str, ...] = ()


class CallRecord(BaseModel):
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    cost_usd: float


class RetrievedRef(BaseModel):
    chunk_id: str
    score: float


class GraphRecord(BaseModel):
    path: list[str]
    tier: str
    verifier: str | None
    verified: bool | None
    escalated: bool
    clarified: bool
    office_lookup: bool = False


class CrawlRecord(BaseModel):
    planned: int
    fetched: int = 0
    embedded: int = 0
    errors: int = 0
    skipped: int
    empty: int = 0
    deleted: int = 0
    remaining: int = 0
    store_chunks: int = 0
    elapsed_s: float = 0.0
    embed_tokens: int = 0
    cost_usd: float = 0.0
    stopped_by: str = "done"
    estimate_fetch_s: float
    estimate_embed_s: float


class RunRecord(BaseModel):
    command: str
    started_at: str
    dry_run: bool
    max_usd: float
    question_hash: str | None = None
    history_hash: str | None = None
    retrieved: list[RetrievedRef] = []
    refused: bool | None = None
    dropped_citations: int = 0
    markdown_stripped: int = 0
    scrubbed: int = 0
    budget_exceeded: bool = False
    pages_indexed: int | None = None
    chunks_indexed: int | None = None
    errors: list[str] = []
    calls: list[CallRecord] = []
    total_cost_usd: float = 0.0
    graph: GraphRecord | None = None
    crawl: CrawlRecord | None = None
    bot_event: str | None = None
