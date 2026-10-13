# Zchut Agent

Zchut Agent answers questions about Israeli bureaucracy (unemployment benefits, pension, Bituach
Leumi, tax refunds) in Hebrew, grounded only in public pages from kolzchut.org.il and gov.il. Every
answer carries numbered citations `[1]`, `[2]` that link to the source pages and their last-updated
dates; when retrieval is too weak to ground an answer it says so in one fixed sentence instead of
guessing. Every AI call is recorded (model, tokens, cost, latency) under `runs/`.

**Status: v1 shipped** (ingest + ask + eval, CLI). Spec:
`agent-os/specs/2026-10-13-zchut-v1-ingest-ask/spec.md`.

The project is spec-driven (Agent OS v3): product docs in `agent-os/product/`, coding standards in
`agent-os/standards/`, one spec per version in `agent-os/specs/`. Every acceptance criterion has a
test whose name quotes it, and `tests/test_spec_coverage.py` fails when one is missing.

## Stack

- Python 3.11, Typer CLI, ruff + mypy strict, pytest (offline only).
- LlamaIndex for ingestion and retrieval; Chroma as the local vector store (v1), Qdrant Cloud free
  tier later (v3).
- Voyage AI `voyage-3.5-lite` embeddings. Rate limits: Voyage's free tier (no payment method)
  allows about 3 requests and 10,000 tokens per minute, so the embedder batches chunks under
  `embed.max_batch_tokens`, paces requests `embed.min_interval_s` (20 s) apart and backs off on
  429/5xx (honouring `Retry-After`); a six-page ingest takes about two minutes.
- Anthropic Haiku by default (Sonnet behind a flag); OpenRouter for open-weights models (Qwen) as
  the cheap tier (v2); LangGraph for the conversation graph (v2).
- Sources: kolzchut.org.il (Creative Commons BY-SA 3.0) and gov.il, fetched under robots.txt with
  a rate limit. Each chunk stores its URL, title, section path, fetch date and license.
  Redirects are followed on the same host only (at most 3); cross-host redirects are refused.
  v1 ingests כל זכות only, because gov.il service pages are client-rendered; the fetcher honours
  the robots.txt `Crawl-delay` (5 s on kolzchut) on top of its 1 s per-host minimum.
  כל זכות pages are read through the MediaWiki API (article HTML plus the revision timestamp as
  the last-updated date) over Python's standard-library HTTP client, because the site's bot
  protection blocks httpx. The User-Agent identifies the project with a contact address.
  The full site is about 7,400 articles; full ingest via the API's `allpages` list is a later
  version.

## Running it

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -e ".[dev]"
pytest                                               # offline: no network, no keys

# --dry-run uses the fake embedder, the in-memory store and a canned answer model
zchut ingest --from-fixtures tests/fixtures/pages --dry-run
zchut ask "מה צריך להביא לרישום בלשכת התעסוקה?" --dry-run
zchut eval --dry-run                                 # add --retrieval-only to skip answers

# real run: Voyage + Chroma (.chroma/) + Haiku; needs the keys below
zchut ingest                                         # fetch config/zchut.json sources
zchut ask "מה התנאים לקבלת דמי אבטלה?" --k 5
zchut eval --max-usd 0.10
```

Environment variables (see `.env.example`; never commit a `.env`): `VOYAGE_API_KEY`,
`ZCHUT_ANTHROPIC_API_KEY` (fallback `ANTHROPIC_API_KEY`), `OPENROUTER_API_KEY` (optional, v2).
Settings live in `config/zchut.json` (sources, chunk size, `retrieval.k`, `retrieval.min_score`,
default cap) and prices in `config/prices.json`.

Every command writes `runs/<YYYY-MM-DD-HHMMSS>-<command>.json` (gitignored): per-call role, model,
tokens, latency and cost, the retrieved chunk ids and scores, and a hash of the question, never its
text. `zchut eval` also writes `...-eval-report.json` with one row per question.

## Eval

`eval/questions.json` holds 30 Hebrew questions: 24 with expected source URLs (the three fixture
pages) and 6 out-of-scope questions that should be refused. Dry run on the fixture index (fake
embedder, so these numbers check the pipeline, not retrieval quality):

| metric | dry run |
| --- | --- |
| hit@3 | 1.00 |
| citation precision | 0.83 |
| refusal rate (expected 0.20) | 0.13 |

## Budget rules

- Under 10 USD per month, all providers together. Anthropic has a console hard limit; Voyage and
  OpenRouter are prepaid; Chroma and the Qdrant free tier cost nothing.
- Every command takes `--max-usd` (default 0.05). Before each embed or answer call the guard
  estimates its cost from the prompt size and `config/prices.json`; if it would cross the cap the
  command stops before the call with a Hebrew message, exit code 2 and `budget_exceeded: true` in
  the run record. A full 30-question eval on Haiku needs about `--max-usd 0.10`.
- Nothing is re-embedded unless the page changed (weekly re-ingest arrives with v4).

## Attribution

Text from kolzchut.org.il is licensed CC BY-SA 3.0; answers that use it say so and link to the
page. gov.il content is public government information. This assistant explains public rules and
is not legal advice.
