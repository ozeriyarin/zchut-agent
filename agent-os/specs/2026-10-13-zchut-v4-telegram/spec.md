# Spec: zchut v4 — Telegram bot on a scale-to-zero host, Qdrant Cloud, per-user sessions

Status: shaped 2026-10-13. Builds on v2/v3/v4a. Standards applied: python-style, secrets-and-config, ai-calls, testing, sources-and-licensing. Shipped 2026-10-14, commit ae6380a; deployed to Cloud Run 2026-10-15 (revision 1).

## Goal

Yarin (and anyone he gives the bot link to) talks to the v2 conversation graph on Telegram and gets an answer within seconds, with citations rendered as links. No always-on process on the owner's machine, no polling: Telegram pushes each message to a webhook on a host that scales to zero and costs nothing at this volume. The index moves from the local Chroma folder to Qdrant Cloud's free tier behind the existing store interface, so the webhook and the owner's CLI read the same corpus. Each Telegram user has a short-lived session (clarification state and the last turns) and nothing about the conversation is kept after the session ends.

## Design

- **Host**: Google Cloud Run, one container (`Dockerfile`, FastAPI + uvicorn, Python 3.11), `--min-instances 0 --max-instances 1 --concurrency 4 --memory 1Gi`, region `europe-west1`. Cold start is 3–8 s, hidden by sending Telegram `sendChatAction: typing` first. Free tier: 2 M requests and 360 k GB-seconds a month; this bot uses well under 1 % of that. **Requires billing enabled on the Google project (a card on file) even inside the free tier**; the owner sets a budget alert at 2 USD and `max-instances 1` caps the worst case. Alternative evaluated and rejected: Vercel Python functions (250 MB unzipped limit is below our dependency set), Cloudflare Workers Python (no langgraph), Render free web service (50 s cold starts).
- **Webhook** (`src/zchut/bot/`): `POST /telegram/<secret-path>` validates the `X-Telegram-Bot-Api-Secret-Token` header against `TELEGRAM_WEBHOOK_SECRET`, answers 200 immediately after enqueueing the update in-process, and processes it: parse message → load session → run the graph (`converse`) → reply with `render_html` (v4a) via `sendMessage` (parse_mode HTML, link previews off). A clarification interrupt is stored in the session; the next message from that user resumes the graph. `/start` and `/help` reply with a fixed Hebrew text listing what the bot covers, the licence line, and that answers are information, not legal advice. Messages from chats not in `TELEGRAM_ALLOWED_CHAT_IDS` (comma-separated; empty = everyone) get a one-line refusal and are counted.
- **Sessions**: Firestore (same Google project, free tier) collection `sessions/<chat_id>`: `{ history: [...≤6 turns], pending_clarification, updated_at }` with a TTL policy of 30 minutes; after that the document is deleted by Firestore, so no conversation text outlives a session. Behind a `SessionStore` protocol with an in-memory fake for tests. No message text, chat id or user name goes into run records (records keep hashes, as today).
- **Store**: `QdrantStore` implementing `VectorStore` (collection `zchut`, cosine, 1024 dims, payload = chunk metadata + text) over the `qdrant-client` HTTP API, `QDRANT_URL` + `QDRANT_API_KEY`. `zchut ingest` / `crawl run` write to Qdrant when `store.backend` is `qdrant` in config; Chroma stays for local work. A one-off `zchut store migrate` copies Chroma → Qdrant in batches with a count check. Qdrant Cloud free cluster: 1 GB, enough for the full corpus (~100 k chunks ≈ 400 MB vectors + payload).
- **Records in the cloud**: the webhook writes run records to a Firestore collection `runs` (same shape as `runs/*.json`, hashes only) because the container has no durable disk; `zchut cost --source firestore` reads them. Secrets come from Cloud Run environment variables set from Secret Manager; nothing in the image.
- **Budget**: per-message cap from config (`bot.max_usd_per_message`, default 0.08) and a daily cap (`bot.max_usd_per_day`, default 1.00) computed from the day's Firestore records; over the daily cap the bot replies with a fixed Hebrew "budget for today is used up" line and makes no model call. Together with the Anthropic console limit this bounds the month.
- **Deploy**: `deploy/cloud-run.md` with the exact `gcloud` commands (build with Cloud Build, deploy, set the webhook with `setWebhook` and the secret token, Firestore TTL policy), and a `make deploy` / `scripts/deploy.ps1`. The owner runs them; the agent never deploys.
- **Local dev**: `zchut bot serve --dry-run` runs the FastAPI app locally with fakes; `zchut bot simulate "<text>"` posts a synthetic update to the local app and prints the reply, for tests and manual checks.

## Acceptance criteria

1. A valid update with the correct secret header gets HTTP 200 and produces exactly one `sendChatAction` and one `sendMessage` call whose text is the v4a HTML rendering of the graph's final answer (fake graph deps, stubbed Telegram transport); a wrong or missing secret gets 403 and no Telegram call.
2. A question that triggers clarification stores `pending_clarification` in the session and replies with the clarification text; the user's next message resumes the graph and the final reply cites sources; the session is cleared of the pending state afterwards.
3. `/start` and `/help` reply with the fixed Hebrew text and make no model call; a message from a chat id outside `TELEGRAM_ALLOWED_CHAT_IDS` gets the refusal line, no model call, and is counted in the record.
4. `QdrantStore.replace`, `search`, `delete`, `count` behave like `InMemoryVectorStore` on a stubbed Qdrant client (re-ingest replaces, search returns scores and metadata); `store migrate` copies all chunks and fails loudly if the counts differ.
5. Over the daily budget (fixture records for today summing above the cap) the bot replies with the fixed line and makes no embed or model call; under it, it proceeds.
6. Run records written by the webhook contain the graph path, tier, cost and hashes only: no chat id, user name, message text or history (redaction test).
7. `zchut bot simulate "מי זכאי לדמי אבטלה?" --dry-run` prints an HTML reply with `<a href` sources and exit 0, with no network.
8. `pytest` passes; every criterion has a test whose name quotes it.

## Tasks

1. `src/zchut/bot/` (FastAPI app, update parsing, Telegram client with injected transport, session store protocol + Firestore + fake, budget check), config `bot` block, `.env.example` names (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_ALLOWED_CHAT_IDS`, `QDRANT_URL`, `QDRANT_API_KEY`, `GOOGLE_CLOUD_PROJECT`).
2. `QdrantStore`, `store migrate`, Firestore run records, `cost --source firestore`.
3. `Dockerfile`, `deploy/cloud-run.md`, `scripts/deploy.ps1`, CLI `bot serve|simulate`, tests for criteria 1–7, README "Telegram" section, roadmap closeout.
