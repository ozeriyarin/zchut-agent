# Zchut Agent

Zchut Agent answers questions about Israeli bureaucracy (unemployment benefits, pension, Bituach
Leumi, tax refunds) in Hebrew, grounded only in public pages from kolzchut.org.il and gov.il. Every
answer carries numbered citations `[1]`, `[2]` that link to the source pages and their last-updated
dates; when retrieval is too weak to ground an answer it says so in one fixed sentence instead of
guessing. Every AI call is recorded (model, tokens, cost, latency) under `runs/`.

**Status: v1 in spec.** Nothing runs yet. The v1 spec is
`agent-os/specs/2026-10-13-zchut-v1-ingest-ask/spec.md`.

The project is spec-driven (Agent OS v3): product docs in `agent-os/product/`, coding standards in
`agent-os/standards/`, one spec per version in `agent-os/specs/`. Every acceptance criterion has a
test whose name quotes it, and `tests/test_spec_coverage.py` fails when one is missing.

## Stack

- Python 3.11, Typer CLI, ruff + mypy strict, pytest (offline only).
- LlamaIndex for ingestion and retrieval; Chroma as the local vector store (v1), Qdrant Cloud free
  tier later (v3).
- Voyage AI `voyage-3.5-lite` embeddings.
- Anthropic Haiku by default (Sonnet behind a flag); OpenRouter for open-weights models (Qwen) as
  the cheap tier (v2); LangGraph for the conversation graph (v2).
- Sources: kolzchut.org.il (Creative Commons BY-SA 3.0) and gov.il, fetched under robots.txt with
  a rate limit. Each chunk stores its URL, title, section path, fetch date and license.

## Running it (once v1 lands)

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -e ".[dev]"
pytest                                               # no network, no keys
zchut ingest --from-fixtures tests/fixtures/pages --dry-run
zchut ingest                                         # fetch the configured pages into .chroma/
zchut ask "מה התנאים לקבלת דמי אבטלה?"
zchut eval                                           # hit@3, citation precision, refusal rate
```

Environment variables (see `.env.example`; never commit a `.env`): `VOYAGE_API_KEY`,
`ZCHUT_ANTHROPIC_API_KEY` (fallback `ANTHROPIC_API_KEY`), `OPENROUTER_API_KEY` (optional, v2).

`eval/questions.json` is an empty list until v1 seeds it with 30 questions and their expected
source URLs.

## Budget rules

- Under 10 USD per month, all providers together. Anthropic has a console hard limit; Voyage and
  OpenRouter are prepaid; Chroma and the Qdrant free tier cost nothing.
- Every command takes `--max-usd` (default 0.05) and aborts before the call that would cross it.
- Nothing is re-embedded unless the page changed (weekly re-ingest arrives with v4).

## Attribution

Text from kolzchut.org.il is licensed CC BY-SA 3.0; answers that use it say so and link to the
page. gov.il content is public government information. This assistant explains public rules and
is not legal advice.
