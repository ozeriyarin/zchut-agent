from __future__ import annotations

import os
import pickle
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

from zchut.index.manifest import Manifest
from zchut.index.store import VectorStore
from zchut.types import Chunk, Hit

RRF_K = 60
FUSION_POOL = 50
PREFIXES = frozenset("והבלמשכ")
_TOKEN = re.compile(r"[^\W\d_]+|\d+")
CacheKey = tuple[int, str | None]


def tokenize(text: str) -> list[str]:
    return [_strip_prefixes(word) for word in _TOKEN.findall(text.lower())]


def _strip_prefixes(word: str) -> str:
    while word[0] in PREFIXES and len(word) > 3:
        word = word[1:]
    return word


class KeywordIndex:
    def __init__(self, chunks: Iterable[Chunk]) -> None:
        self._chunks = list(chunks)
        docs = [tokenize(c.section) * 2 + tokenize(c.text) for c in self._chunks]
        self._bm25 = BM25Okapi(docs) if docs else None

    def search(self, question: str, k: int) -> list[Chunk]:
        terms = tokenize(question)
        if self._bm25 is None or not terms:
            return []
        scores = [float(s) for s in self._bm25.get_scores(terms)]
        ranked = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        return [self._chunks[i] for i in ranked[:k] if scores[i] > 0]


@dataclass(frozen=True)
class _Cached:
    key: CacheKey
    index: KeywordIndex


def cache_key(store: VectorStore, manifest: Manifest) -> CacheKey:
    dates = (entry.fetched_at for entry in manifest.articles.values())
    return store.count(), max(dates, default=None)


def load_keyword_index(store: VectorStore, manifest: Manifest, path: Path) -> KeywordIndex:
    key = cache_key(store, manifest)
    try:
        # The pickle is this app's own cache under the gitignored data/ folder, never fetched.
        cached = pickle.loads(path.read_bytes())
        if isinstance(cached, _Cached) and cached.key == key:
            return cached.index
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError):
        pass  # A missing, corrupt or outdated cache file is rebuilt below.
    index = KeywordIndex(store.chunks())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.tmp")
        temp.write_bytes(pickle.dumps(_Cached(key, index)))
        os.replace(temp, path)
    except OSError:
        pass  # The cache only saves time; a read-only disk still gets a working index.
    return index


def fuse(
    vector: Sequence[Hit], keyword: Sequence[Chunk], k: int, pool_only: bool = False
) -> list[Hit]:
    # Keyword-only hits get the pool's lowest vector score; the best vector hit always stays.
    hits = {h.chunk.id: h for h in vector}
    if pool_only:  # keyword ranking may only reorder vector candidates, never inject new ones
        keyword = [c for c in keyword if c.id in hits]
    floor = min((h.score for h in vector), default=0.0)
    for chunk in keyword:
        hits.setdefault(chunk.id, Hit(chunk=chunk, score=floor))
    fused = dict.fromkeys(hits, 0.0)
    for ranking in ([h.chunk.id for h in vector], [c.id for c in keyword]):
        for rank, chunk_id in enumerate(ranking, start=1):
            fused[chunk_id] += 1.0 / (RRF_K + rank)
    top = sorted(fused, key=lambda chunk_id: -fused[chunk_id])[:k]
    if vector and vector[0].chunk.id not in top:
        top[-1] = vector[0].chunk.id
    return [hits[chunk_id] for chunk_id in top]
