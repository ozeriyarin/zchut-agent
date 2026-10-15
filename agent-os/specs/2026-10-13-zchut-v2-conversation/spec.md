# Spec: zchut v2 — LangGraph conversation, verification, escalation, model tiers

Status: shaped 2026-10-13. Builds on v1. Standards applied: python-style, secrets-and-config, ai-calls, testing, sources-and-licensing. Shipped 2026-10-13, commit f52f86f.

## Goal

`zchut ask` becomes a conversation run by a LangGraph state graph instead of a straight line: the question is classified, a missing fact is asked back (human-in-the-loop), the answer is verified against the retrieved chunks before it is shown, and anything the corpus cannot settle is escalated to the right office with its contact page. The answer model sits behind a tier flag (`haiku`, `qwen-openrouter`, `vllm`) and the eval scores every tier on the same questions, with cost per question. v1's refusal-by-threshold stays as the cheap first gate; the verifier is the second.

## Design

- **Graph** (`src/zchut/graph/`): LangGraph `StateGraph` with a typed `ConversationState` (pydantic): `question`, `history` (prior turns, capped at 6), `topic`, `in_scope`, `missing_fact`, `hits`, `draft`, `verdict`, `final`, `escalation`, `calls`. Nodes, in order:
  1. `classify`: one small model call with a JSON schema → `{ topic: unemployment | pension | tax | other, in_scope: bool, missing_fact: str | null }`. `missing_fact` is set only when the question cannot be answered without one concrete fact the user has (e.g. employee or self-employed, age bracket). Out of scope → `refuse`.
  2. `clarify`: when `missing_fact` is set, the graph **interrupts** with one Hebrew question; the CLI prints it and reads the reply (tests resume with a scripted reply). The reply is appended to `history` and `classify` runs again, at most once.
  3. `retrieve`: v1 retrieval; below `retrieval.min_score` → `refuse` (unchanged sentence).
  4. `answer`: v1 prompt and citation handling, now on the selected tier.
  5. `verify`: a second model call with the draft and the retrieved chunks. Verifier tier `haiku` (default): JSON schema `{ supported: bool, unsupported_claims: string[] }`. Verifier tier `jev`: one OpenRouter decisions call (`typesafe/jev-1.13`, choices `supported` / `unsupported`, criteria text in config) and, only when it says `unsupported`, one Haiku call that lists the unsupported claims. The verifier never runs on the answer tier, so the open-weights tier cannot grade itself. `supported: false` → `escalate`.
  6. `escalate`: fixed Hebrew text per topic from `config/offices.json` (office name, what they handle, the public contact page URL; synthetic-free: these are public gov pages, verified by the owner before merge) plus the unsupported claims listed as "לא הצלחתי לאמת:". No model call.
  7. `refuse`: v1 sentence.
  Edges are conditional on state; every node appends its call usage to `state.calls` so the record is complete. Max 2 model calls on the answer tier and 2 on Haiku per question (classify, verify), enforced by the graph shape, not by a counter.
- **Tiers** (`src/zchut/answer/tiers.py`): `AnswerModel` implementations behind `make_answer_model(tier, secrets, settings)`: `haiku` (v1), `qwen-openrouter` (OpenAI-compatible chat completions on `https://openrouter.ai/api/v1/chat/completions`, model `qwen/qwen3-32b` or the id in config, injected httpx client, `OPENROUTER_API_KEY`), `vllm` (same OpenAI-compatible client pointed at `ZCHUT_VLLM_URL`, optional key `ZCHUT_VLLM_API_KEY`; used only when the owner runs a GPU for a demo). All three take the same `Prompt` and return the same `ModelReply` with usage. Prices for the OpenRouter model id and a nominal `vllm` price (0) are added to `config/prices.json`; an unknown id is still a `ConfigError`.
- **CLI**: `zchut ask Q [--tier haiku|qwen-openrouter|vllm] [--verifier haiku|jev] [--no-verify] [--max-usd]`; interactive clarification reads from stdin (`--reply "<text>"` supplies it non-interactively). `zchut chat` opens a loop that keeps `history` between questions until an empty line. `zchut eval [--tier ...] [--tiers haiku,qwen-openrouter]` runs the graph per answer tier (and `--verifiers haiku,jev` per verifier) and reports, per combination: hit@3, citation precision, refusal rate, **verification pass rate**, **escalation rate**, mean cost per question, mean latency; writes one report per tier and a side-by-side Markdown table. `zchut cost [--days N]` sums `runs/` by command and model (the Operations item in the roadmap).
- **Records**: the run record gains `graph: { path: ["classify", "retrieve", ...], tier, verified: bool | null, escalated: bool, clarified: bool }`; history text is never stored (hash only, like the question).
- **Budget**: the cost guard estimates the whole path up front per node (classify + answer + verify) and the per-command default rises to 0.08 USD; an eval over 30 questions on Haiku with verification stays under 0.30 USD.
- **Offline**: a `FakeClassifier` and `FakeVerifier` (deterministic on keywords) plus the v1 `FakeAnswerModel`; LangGraph runs fully in-process; no network in tests. New dependency: `langgraph` (and `langchain-core` it pulls in). No other LangChain packages.

## Acceptance criteria

1. The graph classifies an in-scope unemployment question as `topic: unemployment, in_scope: true` and follows the path classify → retrieve → answer → verify → final; an out-of-scope question follows classify → refuse with the v1 refusal sentence and makes no retrieval or answer call.
2. A question missing a required fact interrupts after `classify` with one Hebrew clarification; resuming with a scripted reply re-classifies once and continues; a second missing fact does not interrupt again (the graph proceeds with its best reading).
3. When the verifier returns `supported: false`, the final output is the escalation text for the topic from `config/offices.json`, including the unsupported claims, and the draft answer is not shown.
4. `--tier qwen-openrouter` sends the same prompt to the OpenAI-compatible endpoint with the configured model id (stubbed transport), prices the reply under that id, and the verifier still runs on its own tier; `--tier vllm` targets `ZCHUT_VLLM_URL`; `--verifier jev` sends one decisions request with the choices `supported` / `unsupported` and calls Haiku for the claims only on `unsupported`.
5. `zchut eval --tiers haiku,qwen-openrouter --dry-run` writes one report per tier plus a side-by-side table with verification pass rate, escalation rate, cost per question and latency per tier.
6. The record stores `graph.path`, `tier`, `verified`, `escalated`, `clarified`, and every call from every node; neither the question nor any history turn appears in the record (redaction test).
7. `--no-verify` skips the verifier (path has no `verify`), and `zchut cost --days 7` sums the fixture records by command and model.
8. `pytest` passes; every criterion has a test whose name quotes it.

## Tasks

1. `src/zchut/graph/` (state, nodes, edges, interrupt handling), fakes, `config/offices.json` (owner verifies the URLs), classify and verify prompts + schemas.
2. `src/zchut/answer/tiers.py`, OpenAI-compatible client, prices, `.env.example` (`ZCHUT_VLLM_URL`, `ZCHUT_VLLM_API_KEY`), settings.
3. CLI (`ask` flags, `chat`, `eval --tiers`, `cost`), records, tests for criteria 1–7, README section "Conversation graph" with a diagram of the nodes and the tier table, roadmap closeout.
