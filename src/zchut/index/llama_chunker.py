from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from typing import Literal

from llama_index.core.node_parser import MarkdownNodeParser, NodeParser, SentenceSplitter
from llama_index.core.schema import Document

from zchut.config import ChunkEngine, ChunkSettings
from zchut.sources.chunk import PATH_SEP, chunk_page, make_chunk
from zchut.types import Chunk, Page

Chunker = Callable[[Page], list[Chunk]]
LlamaEngine = Literal["llama-markdown", "llama-sentence"]


def _lines(page: Page, marks: bool) -> Iterator[str]:
    yield f"# {page.title}" if marks else page.title
    shown: tuple[str, ...] = ()
    for section in page.sections:
        for depth, heading in enumerate(section.headings):
            if shown[: depth + 1] != section.headings[: depth + 1]:
                yield f"{'#' * (depth + 2)} {heading}" if marks else heading
        shown = section.headings
        yield section.text


class LlamaChunker:
    def __init__(self, engine: LlamaEngine, chunk_size: int, chunk_overlap: int) -> None:
        self.engine = engine
        self._parser: NodeParser
        if engine == "llama-markdown":
            self._parser = MarkdownNodeParser(header_path_separator=PATH_SEP)
        else:
            # A per-character tokenizer makes chunk_size a character budget, like max_chars.
            self._parser = SentenceSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                tokenizer=list,
                paragraph_separator="\n\n",
            )

    def chunk(self, page: Page) -> list[Chunk]:
        text = "\n\n".join(_lines(page, marks=self.engine == "llama-markdown"))
        nodes = self._parser.get_nodes_from_documents([Document(text=text)])
        if self.engine == "llama-sentence":
            return [make_chunk(page, i, page.title, n.get_content()) for i, n in enumerate(nodes)]
        chunks: list[Chunk] = []
        for node in nodes:
            header, _, body = node.get_content().partition("\n")
            if not body.strip():
                continue
            parents = str(node.metadata.get("header_path", "")).split(PATH_SEP)
            section = PATH_SEP.join([*filter(None, parents), header.lstrip("#").strip()])
            chunks.append(make_chunk(page, len(chunks), section, body.strip()))
        return chunks


def make_chunker(settings: ChunkSettings, engine: ChunkEngine | None = None) -> Chunker:
    chosen = engine or settings.engine
    if chosen == "section":
        return partial(chunk_page, max_chars=settings.max_chars, min_chars=settings.min_chars)
    return LlamaChunker(chosen, settings.max_chars, settings.overlap).chunk
