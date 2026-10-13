# Tech stack

- **Language / runtime:** Python 3.11, `src/` layout, Typer CLI (`zchut ingest | ask | eval`), ruff + mypy strict.
- **Sources:** public HTML pages from kolzchut.org.il (Creative Commons BY-SA 3.0) and gov.il, fetched with httpx under the site's robots.txt and a fixed rate limit, parsed with BeautifulSoup. Raw HTML cached under `data/raw/` (gitignored); fixtures for tests are trimmed saved pages under `tests/fixtures/`.
- **Ingestion / retrieval:** LlamaIndex (`llama-index-core`) for the document -> node pipeline and the query path; chunks split by section headings with the title path kept as metadata.
- **Embeddings:** Voyage AI `voyage-3.5-lite` (`llama-index-embeddings-voyageai`) behind an `Embedder` interface with an offline fake; `input_type` document for ingest, query for ask.
- **Vector store:** Chroma, local and persistent under `.chroma/` (v1), behind a `VectorStore` interface with an in-memory fake; Qdrant Cloud free tier replaces it in v4 (Telegram) behind the same interface.
- **AI:** Anthropic SDK, `claude-haiku-4-5` by default and Sonnet behind a flag; OpenRouter over httpx for the open-weights tier (Qwen) from v2; LangGraph for the conversation graph from v2. All answer models behind one `AnswerModel` interface with a fake for tests.
- **Delivery:** CLI in v1; Telegram webhook on Cloud Run (scale to zero) in v4; MCP server in v5.
- **Secrets:** environment variables only (`VOYAGE_API_KEY`, `ZCHUT_ANTHROPIC_API_KEY` with fallback `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`), validated once at startup, never committed. `.env` is gitignored; `.env.example` lists the names.
- **Tests:** pytest, offline only. HTML fixtures, fakes for the embedder, the store and the answer model; `tests/test_spec_coverage.py` fails when an acceptance criterion has no test whose name quotes it.
- **Records:** `runs/<timestamp>-<command>.json` (gitignored): question hash, retrieved chunk ids and scores, per-call model, tokens, cost, latency, refusal flag. Never the user's question text beyond the session.
- **Evals:** `eval/questions.json`, 30 questions with expected source URLs; `zchut eval` reports retrieval hit@3, citation precision and refusal rate per model tier.
- **Tooling:** Agent OS v3 (standards, product, specs), Claude Code for implementation.
