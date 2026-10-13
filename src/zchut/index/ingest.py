from __future__ import annotations

from collections.abc import Sequence

from zchut.index.embedder import Embedder, EmbedError
from zchut.index.store import VectorStore
from zchut.run.cost_guard import CostGuard
from zchut.sources.chunk import PATH_SEP, chunk_page
from zchut.sources.fetch import Fetcher, FetchError
from zchut.sources.parse import ParseError, parse_page
from zchut.types import Chunk, IngestReport


def embed_text(chunk: Chunk) -> str:
    return f"{chunk.section}{PATH_SEP}{chunk.text}"


def ingest(
    urls: Sequence[str],
    fetcher: Fetcher,
    embedder: Embedder,
    store: VectorStore,
    guard: CostGuard,
    max_chars: int,
    min_chars: int,
) -> IngestReport:
    pages = 0
    total = 0
    errors: list[str] = []
    for url in urls:
        try:
            page = parse_page(fetcher.fetch_page(url))
            chunks = chunk_page(page, max_chars, min_chars)
            vectors = guard.embed(embedder, [embed_text(c) for c in chunks], "document")
        except (FetchError, ParseError, EmbedError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        store.replace(url, chunks, vectors)
        pages += 1
        total += len(chunks)
    return IngestReport(pages=pages, chunks=total, errors=tuple(errors))
