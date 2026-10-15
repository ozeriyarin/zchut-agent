from __future__ import annotations

import math
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Protocol

import chromadb
from chromadb.api.types import GetResult
from chromadb.config import Settings as ChromaSettings
from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode
from llama_index.core.vector_stores.types import VectorStoreQuery
from llama_index.vector_stores.chroma import ChromaVectorStore
from qdrant_client import QdrantClient
from qdrant_client import models as qm

from zchut.types import Chunk, Hit

COLLECTION = "zchut"
QDRANT_DIMS = 1024
Rows = list[tuple[Chunk, list[float]]]
_META = ("url", "title", "section", "fetched_at", "license", "last_updated")


class VectorStore(Protocol):
    def replace(
        self, url: str, chunks: Sequence[Chunk], vectors: Sequence[list[float]]
    ) -> None: ...

    def delete(self, url: str) -> None: ...

    def search(self, vector: Sequence[float], k: int) -> list[Hit]: ...

    def count(self) -> int: ...

    def chunks(self) -> Iterator[Chunk]: ...


class PageStore(VectorStore, Protocol):
    def page(self, url: str) -> list[Chunk]: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._by_url: dict[str, list[tuple[Chunk, list[float]]]] = {}

    def replace(self, url: str, chunks: Sequence[Chunk], vectors: Sequence[list[float]]) -> None:
        self._by_url[url] = list(zip(chunks, vectors, strict=True))

    def delete(self, url: str) -> None:
        self._by_url.pop(url, None)

    def search(self, vector: Sequence[float], k: int) -> list[Hit]:
        hits = [
            Hit(chunk=chunk, score=cosine(vector, vec))
            for rows in self._by_url.values()
            for chunk, vec in rows
        ]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]

    def count(self) -> int:
        return sum(len(rows) for rows in self._by_url.values())

    def chunks(self) -> Iterator[Chunk]:
        return (chunk for rows in self._by_url.values() for chunk, _ in rows)

    def page(self, url: str) -> list[Chunk]:
        return sorted((chunk for chunk, _ in self._by_url.get(url, [])), key=lambda c: c.id)


def _node(chunk: Chunk, vector: list[float]) -> TextNode:
    metadata = {key: getattr(chunk, key) or "" for key in _META}
    source = RelatedNodeInfo(node_id=chunk.url)
    return TextNode(
        id_=chunk.id,
        text=chunk.text,
        metadata=metadata,
        embedding=vector,
        relationships={NodeRelationship.SOURCE: source},
    )


def _chunk(node_id: str, text: str, metadata: dict[str, object]) -> Chunk:
    meta = {key: str(metadata.get(key) or "") for key in _META}
    return Chunk(
        id=node_id,
        url=meta["url"],
        title=meta["title"],
        section=meta["section"],
        text=text,
        fetched_at=meta["fetched_at"],
        license=meta["license"],
        last_updated=meta["last_updated"] or None,
    )


def _got_chunks(got: GetResult) -> list[Chunk]:
    docs, metas = got["documents"] or [], got["metadatas"] or []
    return [_chunk(i, d, dict(m)) for i, d, m in zip(got["ids"], docs, metas, strict=True)]


class StoreError(Exception):
    pass


class ChromaStore:
    def __init__(self, path: Path) -> None:
        client = chromadb.PersistentClient(
            path=str(path), settings=ChromaSettings(anonymized_telemetry=False)
        )
        self._collection = client.get_or_create_collection(
            COLLECTION, metadata={"hnsw:space": "cosine"}, embedding_function=None
        )
        self._store = ChromaVectorStore(chroma_collection=self._collection)

    def replace(self, url: str, chunks: Sequence[Chunk], vectors: Sequence[list[float]]) -> None:
        self.delete(url)
        self._store.add([_node(c, v) for c, v in zip(chunks, vectors, strict=True)])

    def delete(self, url: str) -> None:
        self._store.delete(ref_doc_id=url)

    def search(self, vector: Sequence[float], k: int) -> list[Hit]:
        result = self._store.query(
            VectorStoreQuery(query_embedding=list(vector), similarity_top_k=k)
        )
        nodes = result.nodes or []
        # LlamaIndex reports exp(-cosine distance); turn it back into cosine similarity.
        scores = [1.0 + math.log(s) for s in result.similarities or []]
        return [
            Hit(chunk=_chunk(n.node_id, n.get_content(), dict(n.metadata)), score=score)
            for n, score in zip(nodes, scores, strict=True)
        ]

    def count(self) -> int:
        return self._collection.count()

    def page(self, url: str) -> list[Chunk]:
        got = self._collection.get(where={"url": url}, include=["documents", "metadatas"])
        return sorted(_got_chunks(got), key=lambda c: c.id)

    def chunks(self, batch: int = 1000) -> Iterator[Chunk]:
        for offset in range(0, self.count(), batch):
            got = self._collection.get(
                include=["documents", "metadatas"], limit=batch, offset=offset
            )
            yield from _got_chunks(got)

    def rows(self, batch: int) -> Iterator[Rows]:
        for offset in range(0, self.count(), batch):
            got = self._collection.get(
                include=["documents", "metadatas", "embeddings"], limit=batch, offset=offset
            )
            docs, metas = got["documents"] or [], got["metadatas"] or []
            # Chroma returns embeddings as a numpy array, whose truth value is ambiguous.
            vecs = [] if got["embeddings"] is None else got["embeddings"]
            yield [
                (_chunk(i, d, dict(m)), [float(x) for x in v])
                for i, d, m, v in zip(got["ids"], docs, metas, vecs, strict=True)
            ]


def _payload_chunks(points: Sequence[qm.Record]) -> list[Chunk]:
    return [
        _chunk(str(p.payload["chunk_id"]), str(p.payload["text"]), p.payload)
        for p in points
        if p.payload is not None
    ]


def _url_filter(url: str) -> qm.Filter:
    return qm.Filter(must=[qm.FieldCondition(key="url", match=qm.MatchValue(value=url))])


class QdrantStore:
    def __init__(self, client: QdrantClient, dims: int = QDRANT_DIMS) -> None:
        self._client = client
        if not client.collection_exists(COLLECTION):
            params = qm.VectorParams(size=dims, distance=qm.Distance.COSINE)
            client.create_collection(COLLECTION, vectors_config=params)
            client.create_payload_index(COLLECTION, "url", qm.PayloadSchemaType.KEYWORD)

    def replace(self, url: str, chunks: Sequence[Chunk], vectors: Sequence[list[float]]) -> None:
        self.delete(url)
        self.upsert(list(zip(chunks, vectors, strict=True)))

    def upsert(self, rows: Rows) -> None:
        points = [
            qm.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, chunk.id)),
                vector=vector,
                payload={"chunk_id": chunk.id, "text": chunk.text}
                | {key: getattr(chunk, key) for key in _META},
            )
            for chunk, vector in rows
        ]
        if points:
            self._client.upsert(COLLECTION, points=points)

    def delete(self, url: str) -> None:
        self._client.delete(COLLECTION, points_selector=qm.FilterSelector(filter=_url_filter(url)))

    def search(self, vector: Sequence[float], k: int) -> list[Hit]:
        found = self._client.query_points(COLLECTION, query=list(vector), limit=k)
        return [
            Hit(
                chunk=_chunk(str(p.payload["chunk_id"]), str(p.payload["text"]), p.payload),
                score=p.score,
            )
            for p in found.points
            if p.payload is not None
        ]

    def count(self) -> int:
        return self._client.count(COLLECTION, exact=True).count

    def page(self, url: str) -> list[Chunk]:
        points, _ = self._client.scroll(COLLECTION, scroll_filter=_url_filter(url), limit=10_000)
        return sorted(_payload_chunks(points), key=lambda c: c.id)

    def chunks(self, batch: int = 1000) -> Iterator[Chunk]:
        offset: qm.ExtendedPointId | None = None
        while True:
            points, offset = self._client.scroll(COLLECTION, limit=batch, offset=offset)
            yield from _payload_chunks(points)
            if offset is None:
                return


def migrate(source: ChromaStore, target: QdrantStore, batch: int = 256) -> int:
    for rows in source.rows(batch):
        target.upsert(rows)
    copied, expected = target.count(), source.count()
    if copied != expected:
        raise StoreError(f"the source holds {expected} chunks but the target holds {copied}")
    return copied
