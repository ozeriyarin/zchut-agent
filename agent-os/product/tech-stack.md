# Tech stack

- **Language / runtime:** Python 3.11, `src/` layout, Typer CLI (`zchut ingest | ask | eval`), ruff + mypy strict.
- **Sources:** public HTML pages from kolzchut.org.il (Creative Commons BY-SA 3.0) and gov.il, fetched with httpx under the site's robots.txt and a fixed rate limit, parsed with BeautifulSoup. Raw HTML cached under `data/raw/` (gitignored); fixtures for tests are trimmed saved pages under `tests/fixtures/`.
- **Ingestion / retrieval:** a hand-written section chunker (split by h2/h3 headings, title path kept as metadata) and Voyage over httpx by default. LlamaIndex supplies the Chroma store wrapper and, behind the `chunk.engine` / `embed.engine` switches, its `MarkdownNodeParser` / `SentenceSplitter` chunkers and `VoyageEmbedding` adapter (v6, measured, not the default). Retrieval is vector by default; BM25 hybrid with reciprocal rank fusion sits behind `retrieval.mode` (v5b, measured, not adopted).
- **Embeddings:** Voyage AI `voyage-3.5-lite` over httpx behind an `Embedder` interface with an offline fake (`llama-index-embeddings-voyageai` as the optional `llama` engine); `input_type` document for ingest, query for ask.
- **Vector store:** Qdrant Cloud free tier is the live store (migrated 2026-10-15; 6,982 articles, 62,904 chunks), behind a `VectorStore` interface with an in-memory fake; Chroma, local under `.chroma/`, stays for comparisons and dry runs.
- **AI:** Anthropic SDK, `claude-haiku-4-5` by default and Sonnet behind a flag; OpenRouter over httpx for the open-weights tier (Qwen) from v2. All answer models behind one `AnswerModel` interface with a fake for tests.
- **Conversation:** LangGraph state graph with condense, classify, clarify, retrieve, answer, verify, escalate and nudge nodes (plus final and refuse).
- **Delivery:** CLI; Telegram webhook (@zchutbot) on Cloud Run, scale to zero, revision 4 live, built by a dedicated build account (v10); read-only MCP stdio server (v5).
- **Secrets:** environment variables only (`VOYAGE_API_KEY`, `ZCHUT_ANTHROPIC_API_KEY` with fallback `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`), validated once at startup, never committed. `.env` is gitignored; `.env.example` lists the names.
- **Tests:** pytest, offline only. HTML fixtures, fakes for the embedder, the store and the answer model; `tests/test_spec_coverage.py` fails when an acceptance criterion has no test whose name quotes it.
- **Records:** `runs/<timestamp>-<command>.json` (gitignored): question hash, retrieved chunk ids and scores, per-call model, tokens, cost, latency, refusal flag. Never the user's question text beyond the session.
- **Evals:** `eval/questions.json`, 30 questions with expected source URLs; 6 multi-turn conversations (`zchut eval --conversations`); `eval/verifier_labels.json` for verifier accuracy; `zchut eval` reports retrieval hit@3, citation precision and refusal rate in a per-tier table.
- **Tooling:** Agent OS v3 (standards, product, specs), Claude Code for implementation.
