# Zchut Agent

Zchut Agent answers questions about Israeli bureaucracy (unemployment benefits, pension, Bituach
Leumi, tax refunds) in Hebrew, grounded only in public pages from kolzchut.org.il and gov.il. Every
answer carries numbered citations `[1]`, `[2]` that link to the source pages and their last-updated
dates; when retrieval is too weak to ground an answer it says so in one fixed sentence instead of
guessing. Every AI call is recorded (model, tokens, cost, latency) under `runs/`.

**Status: v6 and v10 shipped** (v6: LlamaIndex chunker and embedder measured against the
hand-written pipeline, see "Framework comparison"; v10: Cloud Build runs as a dedicated build
account). @zchutbot is live on Cloud Run revision 4 over 62,904 chunks in Qdrant Cloud.
Specs: `agent-os/specs/2026-10-13-zchut-v1-ingest-ask/spec.md`,
`agent-os/specs/2026-10-13-zchut-v2-conversation/spec.md`,
`agent-os/specs/2026-10-13-zchut-v3-full-corpus/spec.md`,
`agent-os/specs/2026-10-13-zchut-v4-telegram/spec.md`,
`agent-os/specs/2026-10-15-zchut-v5-mcp-and-reingest/spec.md`,
`agent-os/specs/2026-10-15-zchut-v5b-hybrid-retrieval/spec.md`,
`agent-os/specs/2026-10-15-zchut-v6-framework-comparison/spec.md`.

The project is spec-driven (Agent OS v3): product docs in `agent-os/product/`, coding standards in
`agent-os/standards/`, one spec per version in `agent-os/specs/`. Every acceptance criterion has a
test whose name quotes it, and `tests/test_spec_coverage.py` fails when one is missing.

## Stack

- Python 3.11, Typer CLI, ruff + mypy strict, pytest (offline only).
- LlamaIndex for ingestion and retrieval; Chroma as the local vector store (v1), Qdrant Cloud free
  tier for the deployed bot (v4).
- FastAPI + uvicorn for the Telegram webhook on Google Cloud Run; Firestore for sessions and
  cloud run records (v4).
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
zchut compare --dry-run                              # section vs LlamaIndex engines, fixture pages

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
  article `status: error` (retried on the next run) and the run continues. A removed stub page (no
  text blocks, only a removal notice) is marked `status: skipped` with its revision, counted in the
  skipped-pages line of plan and run, and not fetched again until its revision changes.
  `--max-minutes` stops between articles (the listing is not counted); `--max-usd` stops before
  the embed call that would cross it (exit code 2). Either way the manifest only lists articles that are in the store.
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
START ──history──> condense ──not a question──> nudge -> END
  │                  │                            ^
  │ first turn       │ question                   │ greeting / reaction
  v                  v                            │
  └──────────────> classify ──────────────────────┘
                     │  ├── out of scope ──────────> refuse -> END
                     │  ├── office question ───────> escalate (office line) -> END
                     │  └── missing fact (once per session) ──> clarify ──reply──> condense
                     v
                  retrieve ──best score < min_score──> refuse -> END
                     │
                     v
                  answer ──(--no-verify)──> final (+ office line when asked) -> END
                     │                        ^
                     v                        │ supported
                  verify ─────────────────────┤
                                              │ not supported
                                              v
                                           escalate -> END
```

- `classify`: one Haiku call with a JSON schema: topic (`unemployment`, `pension`, `tax`,
  `other`), in scope or not, an office lookup flag, and a missing fact as a key from
  `config/clarifications.json` (see Conversations; `condense` runs first when there is history).
- `clarify`: a LangGraph interrupt with the key's fixed Hebrew question. The CLI prints it and reads
  the reply from stdin, or takes it from `--reply`. `condense` folds the reply into the question and
  `classify` runs once more; a second missing fact does not interrupt again.
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

## Conversations

Follow-ups are resolved before anything is searched (spec
`agent-os/specs/2026-10-15-zchut-v4b-conversation-quality/spec.md`). When the session has earlier
turns or a pending clarification, a `condense` node (one Haiku call with a JSON schema, about 300
input tokens) rewrites the latest message into `{ standalone_question, is_reply_to_clarification,
is_question }`: pronouns and ellipsis resolve against the earlier questions ("איפה המשרד שלהם"
after an unemployment question names שירות התעסוקה), a reply to the bot's clarification is folded
into the pending question, and a message that already stands alone is kept as is. The standalone
question drives `classify`, `retrieve` (threshold unchanged), `answer` and `verify`; the record
keeps only its hash. A reaction that is not a question ("מה קשור?") gets one fixed line,
"לא בטוח שהבנתי שאלה. על מה לבדוק עבורך? למשל: דמי אבטלה, פנסיה, החזר מס.", with no search and no
answer call (`bot_event: nudge`). Clarifications come only from `config/clarifications.json`
(`employment_status`, `months_worked`, `monthly_salary`, each with the topics it applies to): the
classifier returns a key or null, the user sees the fixed `question_he`, eligibility, how-to, where
and which-documents questions never get one, and a session asks at most once (`clarified_once`).
"Where is the office" questions with a known topic answer with the office entry from
`config/offices.json` (`graph.office_lookup: true`, not an escalation). Citations are renumbered in
order of first appearance and the sources block follows. `zchut chat` runs the same session flow
as the bot; `zchut ask` stays single-turn. `zchut eval --conversations [--dry-run]` replays the
five conversations in `eval/conversations.json` (paraphrased from the first live session, each
turn expecting `answer`, `clarify:<key>`, `nudge`, `office` or `refuse`, optional
`expected_urls`) and prints turn accuracy; the dry run scores 1.00 (11/11) and runs in the suite.

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

The table also has `markdown free` (share of drafts that needed no Markdown cleanup) and
`verifier accuracy`: each verifier's verdicts scored against the owner's labels in
`eval/verifier_labels.json` (`{"version": 1, "labels": {"q04": {"label": "supported", "reason":
"..."}}}`); it reads `—` while no labels exist. The questions to label are listed in
`eval/review/2026-10-13-verifier-disagreements.md`.

## Hybrid retrieval

General questions should find general pages, so retrieval fuses a keyword ranking with the vector
ranking (spec `agent-os/specs/2026-10-15-zchut-v5b-hybrid-retrieval/spec.md`). The keyword side is
BM25 (`rank-bm25`) over each chunk's text plus its title path, which counts twice. The tokenizer
splits on anything that is not a letter or a digit, keeps numbers, and strips the prefixes
ו/ה/ב/ל/מ/ש/כ while at least three letters remain, so "ובפנסיה" and "פנסיה" match. The index is
built from the store's chunks and cached in `data/keyword-index.pkl`, keyed by the chunk count and
the manifest's newest `fetched_at`; a crawl that changes either rebuilds it on the next start.

Reciprocal rank fusion (`k = 60`) merges the top 50 of each ranking. Every fused hit keeps its
vector score, and refusal still uses the best vector score, so `retrieval.min_score` keeps its
calibration (the best vector hit always stays in the list; a keyword-only hit carries the lowest
vector score of the pool). `retrieval.mode` in `config/zchut.json` is `hybrid` or `vector` (the v1
order, unchanged); `zchut eval --retrieval-only --mode vector|hybrid` and
`zchut eval --conversations --mode ...` override it for one run.

| run | mode | hit@3 | refusal rate | conversation turns |
| --- | --- | --- | --- | --- |
| dry run (fixture index, fake embedder) | vector | 0.63 | 0.13 (expected 0.10) | 1.00 (13/13) |
| dry run (fixture index, fake embedder) | hybrid | 0.63 | 0.13 (expected 0.10) | 1.00 (13/13) |

The dry run only proves the pipeline: three fixture pages, so both modes find the same pages, in a
different order on 27 of 30 questions. The adoption check is the full-corpus table below: hit@3
must not drop and the four pension misses must improve, otherwise `retrieval.mode` stays `vector`.

Measured on the full corpus (62,904 chunks, Qdrant, 30 questions, retrieval only, 2026-10-15):

| mode | hit@3 | refusal rate | note |
| --- | --- | --- | --- |
| vector | 0.85 | 0.13 | default |
| hybrid (RRF, BM25 may inject) | 0.59 | 0.13 | rejected |
| hybrid (BM25 reorders the vector top-50 only) | 0.67 | 0.13 | rejected |

Conclusion: with this tokenizer, BM25 over 63k Hebrew chunks promotes long chunks that repeat the
question's words over the right short section, and the four pension misses did not improve. The
code stays behind `retrieval.mode` for later work on the tokenizer (stemming, stop words) or a
cross-encoder reranker; the default remains `vector`.

## Framework comparison

"Would LlamaIndex have done this better?" gets a number instead of an opinion (spec
`agent-os/specs/2026-10-15-zchut-v6-framework-comparison/spec.md`). LlamaIndex's node parsers and
its Voyage embedding wrapper plug in behind the same seams as the hand-written code (a chunker is
`Page -> list[Chunk]`, the embedder is the `Embedder` protocol), so the store, eval and citations
do not change and every chunk keeps url, title, section path, license and dates.

| engine | what it does differently |
| --- | --- |
| `section` (baseline) | Splits on the page's h2/h3 headings, merges short sibling sections, splits long ones at line and word breaks under `chunk.max_chars`; section = `title > h2 > h3`. |
| `llama-markdown` | Renders the page as Markdown (`#` title, `##`/`###` from the section path) and runs `MarkdownNodeParser`: one chunk per heading, no merging of short sections and no size cap; the section path comes from the parser's `header_path`. |
| `llama-sentence` | Runs `SentenceSplitter` on the plain page text with `chunk_size = chunk.max_chars` characters and `chunk.overlap` overlap: sentence-aware windows that ignore headings, so the section is the page title only. |
| `llama` embedder | `VoyageEmbedding` from `llama-index-embeddings-voyageai` behind the same batching, pacing and retry limits. The wrapper returns vectors only (Voyage's usage block is dropped), so tokens are estimated as chars / 2 and the call is recorded with `cost_estimated: true`. |

`chunk.engine` (`section | llama-markdown | llama-sentence`) and `embed.engine` (`httpx | llama`)
in `config/zchut.json` choose the defaults (`section`, `httpx`). For a manual side-by-side,
`zchut ingest --engine llama-markdown --collection cmp` indexes the configured pages into a named
Chroma collection (the default index is untouched) and `zchut eval --retrieval-only --collection
cmp` scores it. `zchut compare` runs the four combinations on `config.sources` (each into its own
`cmp-<chunk>-<embed>` collection), prints the table and writes `reports/compare-<date>.json`;
`--dry-run` uses the fixture pages, the fake embedder and in-memory stores.

The recommendation line follows one rule in code: adopt an engine only if its hit@3 is at least
the baseline's and its mean best score (over the questions that expect a source) is at least the
baseline's minus 0.02; among several, the highest hit@3 then score wins; otherwise keep the
baseline. Nothing switches by itself: the owner edits `chunk.engine` / `embed.engine` by hand.

Dry run (`zchut compare --dry-run`, three fixture pages, fake embedder, 30 questions):

| engine | chunks | mean chunk chars | hit@3 | mean best score | embed tokens | cost (USD) | elapsed (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| section + httpx | 20 | 139 | 0.63 | 0.50 | 2398 | 0.00000 | 0.1 |
| llama-markdown + httpx | 22 | 125 | 0.63 | 0.50 | 2408 | 0.00000 | 0.1 |
| llama-sentence + httpx | 7 | 468 | 0.63 | 0.46 | 2253 | 0.00000 | 0.1 |
| section + llama | 20 | 139 | 0.63 | 0.50 | 2398 | 0.00000 | 0.1 |

The dry run proves the plumbing only: both embed engines are the same fake there, so the last row
repeats the baseline, and on three pages every engine finds the same pages. Its recommendation
line ("adopt llama-markdown + httpx": hit@3 0.63 against 0.63, mean best score 0.498 against
0.496) shows the rule at work on a tie and means nothing about the real engines. The decision is the
owner's real run on the six calibration pages (well under a cent of Voyage tokens):

| engine | chunks | mean chunk chars | hit@3 | mean best score | embed tokens | cost (USD) | elapsed (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| section + httpx | 83 | 352 | 0.85 | 0.59 | 13698 | 0.00027 | 62.6 |
| llama-markdown + httpx | 79 | 366 | 0.89 | 0.59 | 13491 | 0.00027 | 7.3 |
| llama-sentence + httpx | 55 | 581 | 0.85 | 0.59 | 13462 | 0.00027 | 11.1 |
| section + llama | 83 | 352 | 0.85 | 0.59 | 17258 | 0.00035 (est.) | 9.9 |

Real run, 2026-10-15 (the first row's elapsed time includes fetching the pages at the crawl delay;
the other rows reuse them). The rule recommends `llama-markdown + httpx`: one more question in the
top three out of 30, same mean score, about the same chunk count. One question on six pages is
within noise, so the default stays `section` until the full corpus is re-indexed and the 30
questions are scored on both again; the llama embedder wrapper adds nothing (it is the same Voyage
model behind another client) and its cost is only estimated, so `embed.engine` stays `httpx`.

Why the pipeline was hand-rolled first: every chunk, citation and cost line had to be auditable
end to end, and a few dozen lines of explicit code are easier to read, test and defend than a
framework default. Hebrew kolzchut pages carry their meaning in the section headings, so the
chunker had to keep the `title > h2 > h3` path on every chunk and merge the many short sibling
sections, which no off-the-shelf parser did out of the box. And adopting a framework piece is a
measurement, not a belief: with both pipelines behind the same seams, the same pages and the same
30 questions, LlamaIndex is adopted where it wins and the table says why.

## Prompt rules

The answer prompt asks for plain Hebrew text with no Markdown (no `**`, `#`, bullets or tables),
at most 6 lines before the sources block, numbers and dates as digits, no legal disclaimer (the
escalation text carries it), one sentence saying what the passages do not contain followed by
what they do say, and a final proofread. `clean_answer` strips any `**` and leading `#`/`-`/`*`
markers the model still emits and collapses blank lines; the run record counts them as
`markdown_stripped`. The CLI prints plain text; `render_html` builds Telegram HTML (escaped
text, `<a href>` sources with their dates, `<b>` header) from the same `Answer`. The verifier
prompt and the Jev criteria say what is unsupported (no passage states the claim, or a number
or date differs) and what is not (paraphrase, omission, a cautious "it depends").

## Telegram

The bot runs the same conversation graph behind a Telegram webhook on Google Cloud Run (scales to
zero, `--max-instances 1`); the corpus lives in Qdrant Cloud's free tier and sessions in Firestore.
Spec: `agent-os/specs/2026-10-13-zchut-v4-telegram/spec.md`; deploy steps: `deploy/cloud-run.md`
and `scripts/deploy.ps1` (the owner runs them).

- `POST /telegram/<hook id>` (the first 16 hex chars of `sha256(TELEGRAM_WEBHOOK_SECRET)`) checks
  the `X-Telegram-Bot-Api-Secret-Token` header (403 otherwise), answers 200 and handles the update
  in a background task: `sendChatAction typing`, then the graph, then one `sendMessage` with the
  `render_html` answer (HTML, link previews off).
- `/start` and `/help` reply with a fixed Hebrew text (topics, sources, CC BY-SA line, "information,
  not legal advice") and make no model call. Chats outside `TELEGRAM_ALLOWED_CHAT_IDS` (empty =
  everyone) get one refusal line; the record counts them as `bot_event: denied`.
- A clarification is stored in the session (`pending_clarification`, `pending_fact`); the next
  message goes through `condense` (see Conversations). Sessions (`sessions/<chat_id>`: last 6
  standalone questions, the pending question, `clarified_once`, `updated_at`, `expires_at`) expire 30 minutes after the last message through a
  Firestore TTL policy, and an expired session is never read back.
- Budget: `bot.max_usd_per_message` (0.08) per message and `bot.max_usd_per_day` (1.00) summed
  from today's records; over the daily cap the bot replies "התקציב של היום נוצל" with no embed or
  model call.
- Records go to the Firestore `runs` collection with the graph path, tier, cost and hashes only:
  no chat id, user name, message text or history. `zchut cost --source firestore` sums them.
- Store: `store.backend` in `config/zchut.json` is `chroma` (local) or `qdrant` (`QDRANT_URL`,
  `QDRANT_API_KEY`; collection `zchut`, cosine, 1024 dims). `zchut store migrate --from chroma --to
  qdrant` copies the local index and fails when the counts differ. The webhook always reads Qdrant.

```bash
zchut bot simulate "מי זכאי לדמי אבטלה?" --dry-run   # one synthetic update in-process, prints the HTML
zchut bot serve --dry-run --port 8080                 # local webhook with fakes; prints Telegram calls
zchut bot serve                                       # production wiring (Firestore, Qdrant, Telegram)
```

New environment variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
`TELEGRAM_ALLOWED_CHAT_IDS`, `QDRANT_URL`, `QDRANT_API_KEY`, `GOOGLE_CLOUD_PROJECT`.

## MCP server

`zchut mcp serve` runs a Model Context Protocol server over stdio (official `mcp` Python SDK,
`FastMCP`), so Claude Code, Claude Desktop or another agent can search the כל זכות index directly.
It reads the store chosen by `store.backend` (Qdrant or Chroma) and embeds queries with Voyage
(`VOYAGE_API_KEY`, plus `QDRANT_URL` and `QDRANT_API_KEY` for Qdrant); a missing key stops it at
startup with the variable name.

- `search(query, k=5, topic=None)`: vector search, no model call. Returns up to `k` results (capped
  at 20), each `{title, section, url, last_updated, score, text, license, attribution}`. The query
  is at most 500 characters. `topic` is a key of `mcp.topics` in `config/zchut.json`
  (`unemployment`, `pension`, `tax`), which keeps only chunks whose title path contains one of its
  substrings; an unknown topic is an error that lists the known ones.
- `fetch(url)`: the page's chunks in section order with the same fields (no score), read from the
  index only; an unknown URL is an error.
- Resource `zchut://stats`: `{chunks, articles, last_crawl, skipped}` from the store count,
  `data/manifest.json` and the newest `runs/*-crawl.json`.
- Every result carries the CC BY-SA attribution line, and the server instructions tell the client
  to cite the URL of every result it uses.

Read-only guarantee: the tools are marked `readOnlyHint`; they only embed the query and read the
configured index. No argument reaches a shell, the filesystem or the network beyond that index,
nothing is written, and results are corpus text, described to the client as data, not
instructions. Queries are not recorded; each call logs one counter line to stderr.

```powershell
claude mcp add zchut -- C:\zchut-agent\.venv\Scripts\zchut.exe mcp serve
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "zchut": {
      "command": "C:\zchut-agent\.venv\Scripts\zchut.exe",
      "args": ["mcp", "serve"]
    }
  }
}
```

The server switches to the repo folder on start, so it reads `config/zchut.json`, `.env`, `data/`
and `runs/` there whatever folder the client launches it from.

## Weekly re-ingest routine

`zchut crawl run` is incremental, so a weekly run only fetches and embeds changed articles.

- `zchut crawl report` reads the last two `runs/*-crawl.json` records and `data/manifest.json` and
  prints a Hebrew report: new, changed, saved, removed, unchanged, skipped and failed counts, the
  articles and chunks in the index (with the chunk change since the previous crawl), elapsed time,
  tokens, cost, why it stopped, and the ten most recently updated titles by `last_updated`.
- `zchut crawl report --notify` also posts the report to Telegram and sets `report_posted: true`
  in the latest crawl record.
- `zchut notify "<text>"` sends one HTML-escaped Telegram message to the single chat id in
  `TELEGRAM_ALLOWED_CHAT_IDS` with `TELEGRAM_BOT_TOKEN`; it refuses when that list is empty or
  holds more than one id.

The re-ingest runs as a **local** scheduled task of the Claude desktop app on the owner's
machine ("zchut weekly re-ingest (local)", Sundays 09:00 Asia/Jerusalem; if the app is closed it
runs on the next launch). A cloud routine was tried on 2026-10-15 and cannot work: kolzchut's
Cloudflare front answers 403 to requests from datacenter addresses, so only a home connection can
crawl the site. The task pulls `main`, runs `zchut crawl run --max-minutes 60 --max-usd 0.30`
against Qdrant Cloud, posts the report with `zchut crawl report --notify`, and commits
`data/manifest.json` and the new `runs/*-crawl.json`.

State between runs: `data/manifest.json` (public page titles, revision ids, dates and counts) and
the `runs/*-crawl.json` records (counts only) are committed, so each run plans against the last
one and the report can diff them. The Claude GitHub App must be installed on the repo for the push.

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
