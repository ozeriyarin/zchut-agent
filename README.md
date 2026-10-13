# Zchut Agent

Zchut Agent answers questions about Israeli bureaucracy (unemployment benefits, pension, Bituach
Leumi, tax refunds) in Hebrew, grounded only in public pages from kolzchut.org.il and gov.il. Every
answer carries numbered citations `[1]`, `[2]` that link to the source pages and their last-updated
dates; when retrieval is too weak to ground an answer it says so in one fixed sentence instead of
guessing. Every AI call is recorded (model, tokens, cost, latency) under `runs/`.

**Status: v3 shipped** (full כל זכות corpus: incremental, resumable crawl; see "Full corpus").
Specs: `agent-os/specs/2026-10-13-zchut-v1-ingest-ask/spec.md`,
`agent-os/specs/2026-10-13-zchut-v2-conversation/spec.md`,
`agent-os/specs/2026-10-13-zchut-v3-full-corpus/spec.md`.

The project is spec-driven (Agent OS v3): product docs in `agent-os/product/`, coding standards in
`agent-os/standards/`, one spec per version in `agent-os/specs/`. Every acceptance criterion has a
test whose name quotes it, and `tests/test_spec_coverage.py` fails when one is missing.

## Stack

- Python 3.11, Typer CLI, ruff + mypy strict, pytest (offline only).
- LlamaIndex for ingestion and retrieval; Chroma as the local vector store (v1), Qdrant Cloud free
  tier later (v4).
- Voyage AI `voyage-3.5-lite` embeddings. Rate limits: Voyage's free tier (no payment method)
  allows about 3 requests and 10,000 tokens per minute, so the embedder batches chunks under
  `embed.max_batch_tokens`, paces requests `embed.min_interval_s` (20 s) apart and backs off on
  429/5xx (honouring `Retry-After`); a six-page ingest takes about two minutes.
- Anthropic Haiku by default; OpenRouter for open-weights models (Qwen) as the cheap tier and for
  the Jev verifier; a self-hosted vLLM server as an optional tier; LangGraph for the conversation
  graph (see "Conversation graph").
- Sources: kolzchut.org.il (Creative Commons BY-SA 3.0) and gov.il, fetched under robots.txt with
  a rate limit. Each chunk stores its URL, title, section path, fetch date and license.
  Redirects are followed on the same host only (at most 3); cross-host redirects are refused.
  v1 ingests כל זכות only, because gov.il service pages are client-rendered; the fetcher honours
  the robots.txt `Crawl-delay` (5 s on kolzchut) on top of its 1 s per-host minimum.
  כל זכות pages are read through the MediaWiki API (article HTML plus the revision timestamp as
  the last-updated date) over Python's standard-library HTTP client, because the site's bot
  protection blocks httpx. The User-Agent identifies the project with a contact address.
  The full site is about 7,370 articles; `zchut crawl` indexes all of them (see "Full corpus").

## Running it

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -e ".[dev]"
pytest                                               # offline: no network, no keys

# --dry-run uses the fake embedder, the in-memory store and a canned answer model
zchut ingest --from-fixtures tests/fixtures/pages --dry-run
zchut ask "מה צריך להביא לרישום בלשכת התעסוקה?" --dry-run
zchut ask "כמה אקבל דמי אבטלה?" --dry-run --reply "שכיר"   # clarification answered up front
zchut eval --dry-run                                 # add --retrieval-only to skip answers
zchut eval --dry-run --tiers haiku,qwen-openrouter --verifiers haiku,jev
zchut chat --dry-run                                 # several questions; an empty line ends it

# real run: Voyage + Chroma (.chroma/) + Haiku; needs the keys below
zchut ingest                                         # fetch config/zchut.json sources
zchut ask "מה התנאים לקבלת דמי אבטלה?" --k 5
zchut eval --max-usd 0.30
zchut cost --days 7                                  # sum runs/ by command and model
zchut crawl plan                                     # whole-site diff against data/manifest.json
zchut crawl run --max-minutes 120 --max-usd 0.50     # resumable; see "Full corpus"
```

Environment variables (see `.env.example`; never commit a `.env`): `VOYAGE_API_KEY`,
`ZCHUT_ANTHROPIC_API_KEY` (fallback `ANTHROPIC_API_KEY`), `OPENROUTER_API_KEY` (optional: Qwen tier
and Jev verifier), `ZCHUT_VLLM_URL` and `ZCHUT_VLLM_API_KEY` (optional: vLLM tier).
Settings live in `config/zchut.json` (sources, chunk size, `retrieval.k`, `retrieval.min_score`,
default cap) and prices in `config/prices.json`.

Every command writes `runs/<YYYY-MM-DD-HHMMSS>-<command>.json` (gitignored): per-call role, model,
tokens, latency and cost, the retrieved chunk ids and scores, and a hash of the question, never its
text. `zchut ask` and `zchut chat` records add `graph` (`path`, `tier`, `verifier`, `verified`,
`escalated`, `clarified`) and a hash of the conversation history, never the turns themselves.
`zchut eval` also writes `...-eval-report.json` with one row per question (one report per tier and
verifier, `...-eval-<tier>-<verifier>-report.json`, when it compares several) and
`...-eval-comparison.md` with the side-by-side table. `zchut cost --days N` reads `runs/` and
prints the spend per command and per model; it makes no calls and writes nothing.

## Full corpus

`zchut crawl` indexes every כל זכות article instead of the six configured pages and keeps the index
current: a later run fetches and embeds only articles whose revision changed.

- `zchut crawl plan` lists every namespace-0, non-redirect article through the MediaWiki
  `allpages` API (500 titles per request, titles starting with a `crawl.skip_prefixes` entry
  dropped), reads the latest revision of each (revisions are fetched with POST, 50 titles per
  request), and diffs that against `data/manifest.json`: new, changed (revision differs, or the
  last attempt failed), unchanged, and gone (in the manifest, no longer listed). It prints the counts and a time estimate. Listing
  costs about 165 requests, about 14 minutes at the 5 s crawl delay; it makes no AI call.
- `zchut crawl run [--limit N] [--max-minutes M] [--max-usd X]` plans the same way, removes gone
  articles from the store and the manifest, then processes new and changed articles in title order:
  fetch (one API request, the revision date is already known), parse, chunk, embed, replace in
  Chroma, and only then write the article's manifest entry. Chunks of consecutive articles share
  embedding requests up to `embed.max_batch_tokens`. A fetch, parse or embed error marks the
  article `status: error` (retried on the next run) and the run continues. `--max-minutes` stops
  between articles (the listing is not counted); `--max-usd` stops before the embed call that
  would cross it (exit code 2). Either way the manifest only lists articles that are in the store.
- The record `runs/<ts>-crawl.json` holds the counts (planned, fetched, embedded, errors, skipped,
  deleted, remaining), the chunk count in the store, elapsed seconds, embed tokens and cost, why it
  stopped, and the estimate used; never a title, a URL or article text.
- `data/` (the manifest and the raw API responses) and `.chroma/` are gitignored local state.

Pace maths, free tier, from `config/zchut.json`:

| step | rule | full crawl (7,370 articles) |
| --- | --- | --- |
| fetch | one request per article at the robots `Crawl-delay` (`crawl.fetch_delay_s`, 5 s) | 7,370 x 5 s = 10.2 h |
| embed | `tokens_per_article` (3,000) x articles / `embed.max_batch_tokens` (8,000) x `embed.min_interval_s` (20 s) | 2,764 requests = 15.4 h |
| wall time | embedding waits overlap the fetches, so the larger of the two | about 10-12 h if Voyage counts fewer tokens than the conservative estimate, at most about 15 h |

`crawl plan` prints this estimate for the articles actually pending. Voyage's first 200 M tokens
are free, but the cost guard prices tokens at list price (0.02 USD per million), so a full first
crawl (about 22 M estimated tokens) needs `--max-usd 0.50`; a normal weekly run of changed articles
fits the default cap. The store ends near 100 k chunks (about 400 MB in `.chroma/`).

Resume: run it in sittings, e.g. `zchut crawl run --max-minutes 120 --max-usd 0.50` each evening,
or once overnight. Every run re-plans and continues where the last one stopped; a crash or Ctrl+C
costs at most the articles waiting in the current embedding batch, which are fetched again next
time. To start over, delete `data/manifest.json` and `.chroma/`. A corrupt manifest stops the
command instead of being overwritten.

Paid tier: adding a card to Voyage lifts the rate limit (tokens stay free up to 200 M); set
`embed.min_interval_s` to 0 and the crawl becomes fetch-bound at about 10 h. The 5 s crawl delay
belongs to the site and cannot be bought down.

After a full crawl, `zchut eval --retrieval-only` checks that hit@3 on the 30 questions stays at
0.90 or above.

## Conversation graph

`zchut ask` runs a LangGraph state graph (`src/zchut/graph/`) over a typed `ConversationState`:

```
            ┌──────────── out of scope ───────────────┐
            │                                          v
START -> classify --missing fact (once)--> clarify   refuse -> END
            │  ^                              │         ^
            │  └──────── reply ───────────────┘         │ best score < min_score
            v                                           │
         retrieve ──────────────────────────────────────┘
            │
            v
          answer --(--no-verify)--> final -> END
            │                        ^
            v                        │ supported
          verify ────────────────────┤
                                     │ not supported
                                     v
                                  escalate -> END
```

- `classify`: one Haiku call with a JSON schema: topic (`unemployment`, `pension`, `tax`,
  `other`), in scope or not, and a missing fact as one Hebrew question.
- `clarify`: a LangGraph interrupt. The CLI prints the question and reads the reply from stdin, or
  takes it from `--reply`. The reply joins the history and `classify` runs once more; a second
  missing fact does not interrupt again.
- `retrieve` and `answer` are the v1 steps on the chosen tier; the v1 refusal stays the first gate.
- `verify`: a second model call checks the draft against the retrieved chunks. When it fails,
  `escalate` replaces the draft with the office for the topic and the claims it could not verify.
- Every node appends its calls to the state and the run record; the cost guard estimates the whole
  path (classify, embed, answer, verify) before the first call.

| answer tier (`--tier`) | model | needs |
| --- | --- | --- |
| `haiku` (default) | `claude-haiku-4-5` | `ZCHUT_ANTHROPIC_API_KEY` |
| `qwen-openrouter` | `qwen/qwen3-32b` (`models.qwen`) via OpenRouter chat completions | `OPENROUTER_API_KEY` |
| `vllm` | `models.vllm` on `ZCHUT_VLLM_URL` (OpenAI-compatible base URL ending in `/v1`; start vLLM with `--served-model-name vllm`) | `ZCHUT_VLLM_URL`, optional `ZCHUT_VLLM_API_KEY` |

| verifier (`--verifier`) | calls | needs |
| --- | --- | --- |
| `haiku` (default) | one Haiku call: `{ supported, unsupported_claims }` | `ZCHUT_ANTHROPIC_API_KEY` |
| `jev` | one OpenRouter decisions call to `typesafe/jev-1.13` (`supported` / `unsupported`, criteria in `config/zchut.json` under `graph.jev`); one Haiku call for the claims only on `unsupported` | `OPENROUTER_API_KEY`, `ZCHUT_ANTHROPIC_API_KEY` |

Flags: `zchut ask Q [--tier ...] [--verifier haiku|jev] [--no-verify] [--reply "<text>"]`,
`zchut chat` (same flags, no `--reply`), `zchut eval [--tier ...] [--tiers a,b]
[--verifiers haiku,jev] [--no-verify]`, `zchut cost [--days N]`. `--dry-run` swaps in the keyword
`FakeClassifier` and the citation-checking `FakeVerifier`.

Escalation offices live in `config/offices.json` (topic -> name, what they handle, contact page).
URLs to be verified by the owner: they are the public home pages of the Israeli Employment
Service, the Capital Market, Insurance and Savings Authority, the Israel Tax Authority and
Bituach Leumi.

## Eval

`eval/questions.json` holds 30 Hebrew questions: 24 with expected source URLs (the three fixture
pages) and 6 out-of-scope questions that should be refused. Dry run on the fixture index (fake
embedder, so these numbers check the pipeline, not retrieval quality):

| metric | dry run |
| --- | --- |
| hit@3 | 1.00 |
| citation precision | 1.00 |
| refusal rate (expected 0.20) | 0.43 |
| verification pass rate | 1.00 |
| escalation rate | 0.00 |

The refusal rate is high in the dry run because `retrieval.min_score` (0.455) was calibrated on
Voyage scores, not on the fake embedder. `--tiers` and `--verifiers` add one row per combination to
the side-by-side table with cost per question and latency.

After the metrics `zchut eval` prints a `כיול:` line with the lowest top score among answerable
questions and the highest among refusal questions; set `retrieval.min_score` in
`config/zchut.json` to the suggested midpoint it prints (if it reports overlap, no threshold
separates them and retrieval needs work first).

## Budget rules

- Under 10 USD per month, all providers together. Anthropic has a console hard limit; Voyage and
  OpenRouter are prepaid; Chroma and the Qdrant free tier cost nothing.
- Every command takes `--max-usd` (default 0.08). `ask` and `chat` first estimate the whole graph
  path (classify, embed, answer, verify); then, before each call, the guard estimates its cost from
  the prompt size and `config/prices.json`; if it would cross the cap the command stops before the
  call with a Hebrew message, exit code 2 and `budget_exceeded: true` in the run record. A full
  30-question eval on Haiku with verification needs about `--max-usd 0.30`. `zchut cost --days 7`
  shows the week's spend.
- Nothing is re-embedded unless the page changed: `zchut crawl run` skips unchanged revisions.

## Attribution

Text from kolzchut.org.il is licensed CC BY-SA 3.0; answers that use it say so and link to the
page. gov.il content is public government information. This assistant explains public rules and
is not legal advice.
