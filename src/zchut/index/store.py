from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import chromadb
from chromadb.config import Settings as ChromaSettings
from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode
from llama_index.core.vector_stores.types import VectorStoreQuery
from llama_index.vector_stores.chroma import ChromaVectorStore

from zchut.types import Chunk, Hit

COLLECTION = "zchut"
_META = ("url", "title", "section", "fetched_at", "license", "last_updated")


class VectorStore(Protocol):
    def replace(
        self, url: str, chunks: Sequence[Chunk], vectors: Sequence[list[float]]
    ) -> None: ...

    def delete(self, url: str) -> None: ...

    def search(self, vector: Sequence[float], k: int) -> list[Hit]: ...

    def count(self) -> int: ...


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
