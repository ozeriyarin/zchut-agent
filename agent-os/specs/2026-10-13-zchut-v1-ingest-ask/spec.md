# Spec: Zchut v1 — ingest + ask (CLI, citations, refusal, eval set)

Status: shaped 2026-10-13. Standards applied: python-style, secrets-and-config, ai-calls, testing, sources-and-licensing.

## Goal

`zchut ingest` turns a fixed list of public kolzchut.org.il and gov.il pages into a local vector index where every chunk knows its URL, title, section path, fetch date and license. `zchut ask "<question>"` prints one Hebrew answer with numbered citations that link back to those pages, or a fixed Hebrew refusal when retrieval is too weak to ground an answer. `zchut eval` scores the pipeline on a frozen 30-question set. Every AI call is recorded and every command stops before it would cross its cost cap.

## Scope (v1)

- Sources: a committed URL list in `config/zchut.json` (unemployment benefits, pension, Bituach Leumi, tax refunds; kolzchut first, a few gov.il pages). Fetched with httpx under robots.txt and one request per second per host; raw HTML cached under `data/raw/`.
- Parsing and chunking: BeautifulSoup extracts the title, the `last updated` line when the page has one, and the body as a tree of `h2`/`h3` sections; each section becomes one or more chunks that carry the title path (`<page title> > <h2> > <h3>`). Short sibling sections are merged up to the configured size.
- Index: LlamaIndex nodes with metadata `url`, `title`, `section`, `fetched_at`, `license`, `last_updated`; embeddings from `VoyageEmbedder` (`voyage-3.5-lite`) behind `Embedder`; storage in `ChromaVectorStore` (persistent, `.chroma/`) behind `VectorStore`. `FakeEmbedder` and `InMemoryVectorStore` for tests and `--dry-run`.
- Ask: embed the question (`input_type: query`), top-k chunks (default 5) with scores; refusal in code when the best score is under `retrieval.min_score`; otherwise `HaikuAnswerModel` (`claude-haiku-4-5`) writes the answer from the numbered chunks only. `FakeAnswerModel` for tests. Output: the answer, then a `מקורות` block with `[n] <title> — <url> (עודכן: <D.M.YYYY>)` lines, then the CC BY-SA attribution line when any cited chunk is from kolzchut.
- Eval: `eval/questions.json` with 30 entries `{ id, question, expected_urls, expect_refusal }`; `zchut eval` runs every question through retrieval (and the answer step unless `--retrieval-only`) and reports retrieval hit@3 (an expected URL among the top 3), citation precision (cited URLs that are expected / cited URLs) and refusal rate (refusals / questions, with the expected rate next to it).
- Records: `runs/<YYYY-MM-DD-HHMMSS>-<command>.json`: per-call `{ role, model, input_tokens, output_tokens, cache_read_tokens, latency_ms, cost_usd }`, totals, retrieved chunk ids with scores, refusal flag, errors, question hash. Gitignored.
- Cost guard: `--max-usd` on every command, default 0.05 from `config/zchut.json`; before each call the guard estimates its cost from the price table (`config/prices.json`) and the prompt size and raises `BudgetExceeded` if the running total plus the estimate would cross the cap.
- CLI (Typer): `zchut ingest [--from-fixtures <dir>] [--dry-run] [--max-usd]`, `zchut ask "<question>" [--k 5] [--dry-run] [--max-usd]`, `zchut eval [--retrieval-only] [--dry-run] [--max-usd]`. `--dry-run` uses the fakes and touches no network.

## Out of scope (later roadmap items)

LangGraph conversation, clarification and escalation, Qwen/vLLM tiers, Telegram, Qdrant Cloud, MCP server, weekly re-ingest diff.

## Refusal sentence (fixed, Hebrew)

`לא מצאתי מקור אמין מספיק כדי לענות על זה. מומלץ לפנות ישירות למשרד הרלוונטי.`

## Acceptance criteria

1. `zchut ingest --from-fixtures tests/fixtures/pages --dry-run` indexes the three saved pages (two kolzchut, one gov.il) with the fake embedder and the in-memory store, prints the page and chunk counts, and a page that fails to parse is listed as an error while the other pages are still indexed.
2. Chunking: each fixture page is split by its section headings, every chunk carries the title path (`<page title> > <h2> > <h3>`), short sibling sections are merged, and no chunk exceeds `chunk.max_chars`.
3. Every chunk has `url`, `title`, `section`, `fetched_at` (ISO date) and `license` (`CC-BY-SA-3.0` for kolzchut, `gov.il` for gov.il), plus `last_updated` when the page states it and `None` when it does not.
4. The embedder and the store sit behind interfaces with offline fakes: `FakeEmbedder` is deterministic (same text, same vector), `InMemoryVectorStore` returns top-k by cosine with scores and metadata, ingesting the same URL twice replaces its chunks instead of duplicating them, and `VoyageEmbedder` builds its request with model `voyage-3.5-lite` and `input_type` `document` for ingest and `query` for ask against a stubbed transport.
5. `zchut ask` prints a Hebrew answer whose citations `[1]`, `[2]` map to the retrieved chunks, followed by a `מקורות` block with one line per cited source holding its URL and its last-updated date when present; a citation number the model invents is dropped from the answer and counted in the record.
6. When the best retrieval score is under `retrieval.min_score`, the output is exactly the fixed refusal sentence, no answer-model call is made, and the record marks `refused: true`.
7. `eval/questions.json` holds exactly 30 entries that validate against the schema (`id`, `question`, at least one `expected_urls` entry or `expect_refusal: true`), and `zchut eval --dry-run` over the fixture index reports retrieval hit@3, citation precision and refusal rate and writes an eval report under `runs/`.
8. Every embed and answer call is recorded with role, model, input and output tokens, cache read tokens, latency and a cost computed from `config/prices.json`; the record total equals the sum of per-call costs and never contains an API key or the question text (redaction test).
9. Cost guard: with `--max-usd` set below the estimate of the next call, the command aborts before that call with a Hebrew message and exit code 2, the record shows `budget_exceeded: true`, and the default cap is 0.05.
10. `pytest` passes; every criterion above has a test whose name quotes it.

## Design

```
src/zchut/
  cli.py                     Typer app: ingest, ask, eval; --dry-run, --max-usd
  config.py                  env (pydantic) + config/zchut.json + config/prices.json loaders
  types.py                   Page, Section, Chunk, Hit, Answer, Citation, RunRecord, CallRecord
  sources/fetch.py           Fetcher protocol, HttpxFetcher (robots.txt, rate limit, data/raw cache), FixtureFetcher
  sources/parse.py           parse_page(html, url) -> Page (title, last_updated, sections, license by host)
  sources/chunk.py           chunk_page(page, max_chars) -> list[Chunk] with title paths
  index/embedder.py          Embedder protocol, VoyageEmbedder, FakeEmbedder
  index/store.py             VectorStore protocol, ChromaVectorStore, InMemoryVectorStore
  index/ingest.py            ingest(pages, embedder, store, guard) -> IngestReport
  answer/model.py            AnswerModel protocol, HaikuAnswerModel, FakeAnswerModel
  answer/prompt.py           system prompt (Hebrew), numbered chunk context
  answer/citations.py        parse [n], drop unknown numbers, build the sources block
  answer/refusal.py          REFUSAL_SENTENCE, should_refuse(hits, min_score)
  eval/questions.py          schema + loader for eval/questions.json
  eval/metrics.py            hit_at_k, citation_precision, refusal_rate (pure)
  eval/run_eval.py           runs the set, writes the report
  run/records.py             RunRecord build, redaction, write to runs/
  run/cost_guard.py          CostGuard(max_usd, prices): estimate, check, add
  run/ask.py                 ask orchestration: embed -> search -> refuse or answer -> record
config/zchut.json            sources list, chunk.max_chars, retrieval.k, retrieval.min_score, default max_usd, model
config/prices.json           $ per 1M input / output / cache-read tokens per model, Voyage per 1M tokens
eval/questions.json          30 questions with expected source URLs
tests/fixtures/pages/        two kolzchut pages + one gov.il page (trimmed HTML) + .meta.json each
tests/test_*.py              one file per module, criteria quoted in docstrings
tests/test_spec_coverage.py  every criterion has a quoting test
runs/                        gitignored records and eval reports
```

Answer flow: `embed(question)` -> `store.search(vector, k)` -> `should_refuse` -> `AnswerModel.answer(question, numbered chunks)` -> `citations.resolve` -> print -> record. The cost guard wraps every embed and answer call.

## Tasks

1. Scaffold: `pyproject.toml`, `config/zchut.json`, `config/prices.json`, `.env.example`, `tests/conftest.py`, the three HTML fixtures with `.meta.json`.
2. `types.py`, `config.py` with tests for env validation and config loading.
3. Sources: fetch (robots.txt, rate limit, cache), parse, chunk, with fixture-based tests (criteria 1, 2, 3).
4. Index: embedder (Voyage + fake), store (Chroma + in-memory), ingest (criteria 1, 4).
5. Answer: prompt, Haiku model, fake, citations, refusal (criteria 5, 6).
6. Run: records with redaction, cost guard, ask orchestration (criteria 8, 9), CLI.
7. Eval: seed `eval/questions.json` with 30 questions and expected URLs, metrics, `zchut eval` (criterion 7).
8. README: how to run, the eval table, the budget rules, the attribution line; roadmap closeout.
