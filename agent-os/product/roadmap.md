# Roadmap

Each item is one spec in `agent-os/specs/`. Ship in order; each step is usable on its own.

## Shipped

- **v1: ingest + ask** (`2026-10-13-zchut-v1-ingest-ask`), shipped 2026-10-13. `zchut ingest` fetches kolzchut.org.il and gov.il pages into Chroma through LlamaIndex, chunked by section headings with the title path and per-chunk url/title/section/fetched_at/license. `zchut ask "<question>"` prints a Hebrew answer with numbered citations and the page's last-updated date, or a fixed refusal when the best retrieval score is under the threshold. `eval/questions.json` holds 30 questions with expected source URLs; `zchut eval` reports retrieval hit@3, citation precision and refusal rate. Every AI call is recorded under `runs/`; `--max-usd` (default 0.05) aborts before the call that would exceed it.
- **v2: LangGraph conversation** (`2026-10-13-zchut-v2-conversation`), shipped 2026-10-13. `zchut ask` runs a LangGraph state graph: classify (topic, in scope, missing fact) -> one clarification interrupt (stdin or `--reply`) -> retrieve -> answer -> verify -> final, with refuse for out-of-scope or weak retrieval and escalate to the topic office from `config/offices.json` when verification fails. Answer tiers `--tier haiku | qwen-openrouter | vllm`, verifier tiers `--verifier haiku | jev`, `--no-verify`; `zchut chat` keeps history between questions; `zchut eval --tiers ... --verifiers ...` writes one report per combination and a side-by-side table with verification pass rate, escalation rate, cost per question and latency; `zchut cost --days N` sums `runs/` by command and model. Default `--max-usd` is now 0.08.

## Next

1. **v3: Telegram.** Webhook on a free serverless tier (no polling, no always-on process), Qdrant Cloud free tier replacing local Chroma behind the same store interface, per-user conversation memory with a session TTL and no message content kept past the session.
2. **v4: MCP server + re-ingest routine.** An MCP server exposing `search` and `fetch` tools over the index so any MCP client can use the corpus; a weekly routine that re-fetches the source pages, diffs them against the stored chunks and reports what changed (added, removed, last-updated moved).
3. **gov.il via its data API or a rendered fetch.** gov.il service pages are client-rendered, so v1 ingests kolzchut only; add gov.il through its data API or a headless rendered fetch.

## Operations

- Budget: under 10 USD per month across all providers. Anthropic console hard limit, Voyage prepaid, OpenRouter prepaid; Chroma local and Qdrant Cloud free tier cost nothing.
- Every command takes `--max-usd` (default 0.08) and aborts before the call that would cross it; `zchut eval` over 30 questions on Haiku with verification should stay under 0.30 USD.
- Weekly: sum `runs/` for the week (`zchut cost --days 7`); if the month is on track to exceed 8 USD, switch the default tier to `qwen-openrouter`.
- Re-ingest only on the weekly routine (v4); never re-embed unchanged pages.
