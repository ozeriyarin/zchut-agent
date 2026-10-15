# Spec: zchut v6 — framework comparison: LlamaIndex chunker and embedder against the hand-written pipeline

Status: shaped 2026-10-15. Builds on v1/v5b. Standards applied: python-style, ai-calls, testing. Shipped 2026-10-15, commit a095e75; real run llama-markdown 0.89 vs section 0.85 on six pages, default kept pending a full re-index.

## Goal

Answer "would LlamaIndex have done this better" with a number instead of an opinion. The LlamaIndex node parser and embedding interface are plugged in behind the existing `chunk` and `Embedder` seams, the same six calibration pages are indexed both ways into separate collections, and the same 30 questions are scored on each. The winner becomes the default only if it wins on hit@3 without losing citation precision; either way the README records the table and the reasons.

## Design

- **Chunker adapter** `src/zchut/index/llama_chunker.py`: `LlamaChunker` wraps `llama_index.core.node_parser.MarkdownNodeParser` fed with the page rendered as Markdown headings (`#`/`##`/`###` from the section path) and, as a second variant, `SentenceSplitter(chunk_size, chunk_overlap)` on the plain text. Output is converted to the project's `Chunk` type with the same metadata (url, title, section path where the parser provides one, licence, dates), so the store, eval and citations do not change. Config `chunk.engine: section | llama-markdown | llama-sentence`.
- **Embedder adapter** `src/zchut/index/llama_embedder.py`: `LlamaVoyageEmbedder` wraps `llama_index.embeddings.voyageai.VoyageEmbedding` (the dependency already in pyproject) behind the project's `Embedder` protocol, same batching limits, same cost recording from the usage the wrapper exposes (if it exposes none, estimate from chars/2 and mark the record `cost_estimated: true`). Config `embed.engine: httpx | llama`.
- **Separate collections**: `zchut ingest --engine <chunk-engine> --collection <name>` indexes the six calibration pages (`config.sources`) into a named collection (Chroma locally); `zchut eval --retrieval-only --collection <name>` scores against it. The full corpus is not re-indexed for the comparison.
- **Comparison command** `zchut compare` runs, on the six pages: section + httpx (baseline), llama-markdown + httpx, llama-sentence + httpx, section + llama embedder; prints one table: engine, chunks, mean chunk chars, hit@3, citation precision (retrieval-only skips it: report hit@3 and mean best score), embed tokens, cost, elapsed; writes `reports/compare-<date>.json`. `--dry-run` uses fakes and the fixture pages.
- **Decision rule in code**: `compare` prints a recommendation line: adopt an engine only if hit@3 ≥ baseline and mean best score ≥ baseline − 0.02; otherwise keep the baseline. The owner flips `chunk.engine` by hand; nothing auto-switches.
- **README "Framework comparison"**: the table from the real run (owner runs it; ~cents), what each engine does differently, and the one-paragraph conclusion with the reasons, written to be quotable in an interview.

## Acceptance criteria

1. `LlamaChunker` with the markdown engine converts a fixture page into chunks carrying url, title, section path, licence and dates identical in schema to the section chunker's, and the sentence engine produces chunks within `chunk_size` characters.
2. `LlamaVoyageEmbedder` satisfies the `Embedder` protocol over a stubbed transport, batches like the httpx embedder, and records cost (or marks it estimated).
3. `zchut ingest --engine llama-markdown --collection cmp` indexes the fixture pages into a separate collection and leaves the default collection untouched.
4. `zchut compare --dry-run` prints the four-row table with the documented columns, writes the report, and prints the recommendation line computed by the stated rule.
5. `pytest` passes; every criterion has a test whose name quotes it.

## Tasks

1. Adapters (`llama_chunker.py`, `llama_embedder.py`), config engines, named collections in the Chroma store.
2. `zchut compare`, report, recommendation rule, eval `--collection`.
3. Tests (criteria 1–4), README section with the dry-run table and a placeholder row for the owner's real run, roadmap closeout.
