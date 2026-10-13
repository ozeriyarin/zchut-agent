# Secrets and configuration

- Secrets come from the environment only: `VOYAGE_API_KEY`, `ZCHUT_ANTHROPIC_API_KEY` (fallback `ANTHROPIC_API_KEY`, which Claude Code cloud sessions withhold from the shell) and `OPENROUTER_API_KEY` (optional; enables the Qwen tier). Never in code, specs, fixtures, run records or logs.
- `src/zchut/config.py` reads and validates env once at startup (pydantic) and fails fast with the missing variable name; nothing else touches `os.environ`. A command that needs no provider (fixture-only runs, `zchut eval --dry-run`) must not require any key.
- `.env` is gitignored. `.env.example` lists every variable with an empty value and a one-line purpose.
- Non-secret settings (source URL list, chunk size, retrieval threshold, default model tier, default `--max-usd`) live in `config/zchut.json` and are committed.
- Budget caps live with the provider, not only in code: Anthropic console hard limit, Voyage and OpenRouter prepaid. The code-side guard (`--max-usd`) is the second line, never the only one.
- Run records (`runs/*.json`) are gitignored and must never contain API keys, raw user question text, or anything from a user beyond a question hash; redact at the record boundary.
