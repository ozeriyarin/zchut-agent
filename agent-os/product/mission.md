# Mission

Zchut Agent answers questions about Israeli bureaucracy in Hebrew, grounded only in public pages from kolzchut.org.il and gov.il, with a numbered citation for every claim, so a person facing unemployment, pension, Bituach Leumi or a tax refund gets a sourced answer instead of a guess.

## Users

- Yarin (owner): a full-stack / AI developer in Modi'in building an interview portfolio project that shows RAG, agent graphs and evals done properly on a tiny budget.
- Later: friends and family who ask "what am I entitled to" and want the answer with the page it came from.

## Problems it solves

- Entitlements are documented, but across hundreds of pages written for lawyers; finding the right one takes longer than reading it.
- Chatbots answer bureaucracy questions confidently and wrong; without citations nobody can check.
- The honest answer is often "ask the office"; a good assistant says so and names the office.

## What it does

1. Ingests: fetches public pages (kolzchut.org.il under CC BY-SA, gov.il), splits them by section headings with the title path, and stores each chunk with its URL, title, section, fetch date and license.
2. Retrieves: Voyage embeddings over a local Chroma store (Qdrant Cloud later); every answer starts from the top chunks and their scores.
3. Answers: a cheap model (Haiku by default, Qwen via OpenRouter as a cheaper tier) writes one Hebrew answer with numbered citations [1], [2] linking to the source URLs and the page's last-updated date.
4. Refuses: when the best retrieval score is below the threshold the answer is one fixed Hebrew sentence, never an improvisation.
5. Measures: a frozen 30-question eval set with expected sources reports retrieval hit@3, citation precision and refusal rate; every AI call is recorded with model, tokens, cost and latency under `runs/`.

## Principles

- Spec first: every version starts as a spec in `agent-os/specs/` written against the standards in `agent-os/standards/`.
- Grounded or silent: no sentence without a source chunk behind it; weak retrieval means refusal.
- Public sources, attributed: only pages anyone can read, with the license carried on every chunk.
- Under 10 USD a month: every provider prepaid or hard-limited, every command has a cost guard.
- Every run is auditable: what was retrieved, what was answered, what it cost.
- Secrets and user questions never live in the repo.
