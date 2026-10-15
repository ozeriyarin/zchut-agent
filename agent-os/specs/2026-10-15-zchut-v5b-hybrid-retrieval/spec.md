# Spec: zchut v5b — hybrid retrieval: BM25 fused with vector search

Status: shaped 2026-10-15 from the full-corpus eval (hit@3 0.85; four pension questions where sector-specific pages outrank the general page) and the live session (a wartime unpaid-leave page cited for the general calculation rule). Builds on v1/v3. Standards applied: python-style, testing.

## Goal

General questions find general pages. Retrieval fuses a keyword ranking (BM25 over the chunk text and title path) with the vector ranking, so exact terms and titles count alongside meaning. Measured on the same 30 questions and the 6 conversations; adopted only if hit@3 does not drop and the four pension misses improve.

## Design

- **Keyword index** `src/zchut/index/keyword.py`: BM25 (`rank-bm25` dependency, pure Python) over a simple Hebrew-aware tokenizer (split on non-letters, strip the prefixes ו/ה/ב/ל/מ/ש/כ when the remainder is 3+ letters, keep digits), built from the store's chunks (`store.rows()` for Chroma; a scroll for Qdrant) and cached to `data/keyword-index.pkl` keyed by the store count and the manifest's newest `fetched_at` (rebuilt when either changes; a few seconds for 63 k chunks). Title path tokens are weighted ×2 by repeating them.
- **Fusion**: reciprocal rank fusion with `rrf_k = 60` over the top 50 of each ranking; `retrieval.mode: vector | hybrid` (default `hybrid` after the measurement; `vector` keeps v1 behaviour). The fused list keeps each hit's vector score for the refusal threshold: refusal is decided on the best **vector** score as before, so the threshold calibration still holds; the fused order decides what the model reads.
- **Eval**: `zchut eval --retrieval-only --mode vector|hybrid` reports both on the same questions; the README records the before/after table; the conversations eval runs in hybrid mode too.
- **Bot/CLI**: the mode is read from config at startup; no API change.

## Acceptance criteria

1. The tokenizer strips Hebrew prefixes and keeps digits (table-driven), and the BM25 index ranks a chunk whose title path contains the question's exact words above a chunk that only paraphrases them (fixture).
2. Reciprocal rank fusion merges two rankings deterministically, keeps vector scores on the fused hits, and `retrieval.mode: vector` reproduces the v1 order exactly.
3. The keyword index is cached on disk and rebuilt only when the store count or the manifest's newest fetch date changes (fake store, temp dir).
4. `zchut eval --retrieval-only --mode hybrid` runs on the fixture index and the dry-run metrics do not regress versus `--mode vector` on the fixture questions.
5. `pytest` passes; every criterion has a test whose name quotes it.

## Tasks

1. `keyword.py` (tokenizer, BM25 build, cache), fusion in `run/ask.py` `retrieve`, config `retrieval.mode`.
2. Eval flag, conversations eval in hybrid, README "Hybrid retrieval" section with the measured table (owner runs the real eval; the agent fills the dry-run numbers and leaves the real row for the owner), roadmap closeout.
