# CLAUDE.md

## What this is

Zchut Agent: a Hebrew-first RAG assistant for Israeli bureaucracy over public kolzchut.org.il
(CC BY-SA) and gov.il pages. Python 3.11, LlamaIndex, Chroma (then Qdrant), Voyage embeddings,
Anthropic Haiku/Sonnet, OpenRouter (Qwen), LangGraph from v2. Interview portfolio project by Yarin
Ozeri; hard budget under 10 USD per month.

## Workflow (Agent OS v3)

- Product docs: `agent-os/product/` (mission, roadmap, tech-stack). Read them before any work.
- Standards: `agent-os/standards/` indexed by `index.yml`. Read the ones that apply before coding.
- One spec per version in `agent-os/specs/<date>-zchut-vN-<slug>/spec.md`: Goal, Scope, Acceptance
  criteria (numbered), Design, Tasks. The last criterion is always "`pytest` passes; every
  criterion above has a test whose name quotes it".
- Every criterion gets a test `test_criterion_<n>_<slug>` whose docstring starts with
  `criterion <n>: "<criterion text>"`; `tests/test_spec_coverage.py` enforces it. Remove the
  spec's `UNIMPLEMENTED` entry there when its tests exist.
- Commands in `.claude/commands/agent-os/` (`/plan-product`, `/shape-spec`, standards tools).
- Order: spec -> code + tests -> `pytest`, `ruff check`, `mypy` -> README/roadmap closeout -> commit.

## House rules

- Comments: one line, at most 100 characters, only when the intent is not obvious. No docstring
  essays; the specs and standards hold the explanations.
- Hebrew strings are literal UTF-8 in code, never `\u` escapes. Output is Hebrew-first.
- Tests run offline: no network, no keys; fetcher, embedder, store and answer model have fakes.
- Never commit `runs/`, `data/raw/`, `.chroma/`, `.env` or any key. Run records never hold the
  user's question text or anything personal.
- Only public sources, under robots.txt and rate limits; every chunk keeps url, title, section,
  fetched_at and license.
- Every AI call goes through the cost guard; `--max-usd` default 0.05.
- Commit messages: `<type>: <what it does>` (`feat`, `fix`, `chore`, `docs`), one spec per feature
  commit.
