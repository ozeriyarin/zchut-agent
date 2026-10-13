# AI calls

- One role, several tiers: the answer model is `claude-haiku-4-5` by default, Sonnet only behind an explicit flag, Qwen via OpenRouter as the cheap tier (v2), all behind `AnswerModel`. Never call the SDK directly from `run/` or `cli.py`.
- Every answer is grounded: the prompt holds only the retrieved chunks (with their numbers, URLs and last-updated dates) and the question; the model may cite only those numbers. A citation to a number that was not provided is dropped and counted in the record.
- Refusal is decided in code, not by the model: when the best retrieval score is under `config.retrieval.min_score`, the answer is the fixed Hebrew refusal sentence and no model call is made.
- Prompts are Hebrew-first: the system prompt asks for Hebrew output, short paragraphs, numbered citations `[n]`, and a closing line listing the sources with their last-updated dates when present.
- Every call records model, input/output tokens, cache read tokens, latency and an estimated cost (from `config/prices.json`) into the run record; embedding calls are recorded the same way with role `embed`.
- Budgets: `--max-usd` per command (default 0.08); the cost guard estimates the next call from the price table and the prompt size and aborts with `BudgetExceeded` before sending it. Hard timeout per call, backoff on 429/5xx (Retry-After or 20/40/60 s, up to 5 attempts), no silent fallbacks to a more expensive model.
