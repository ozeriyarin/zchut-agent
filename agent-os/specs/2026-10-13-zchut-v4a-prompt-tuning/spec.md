# Spec: zchut v4a — prompt tuning, scored by the eval table

Status: shaped 2026-10-13. Builds on v2. Standards applied: ai-calls, testing.

## Goal

The answers read like a careful clerk wrote them for a phone screen: plain text, no Markdown, no typos, short, Hebrew throughout, with the citations and the sources block exactly as v1 renders them. The verifier's strictness is understood, not guessed: the answers the two verifiers disagreed on in the first real eval are read, labelled, and the verifier prompt is adjusted only where the label says the verifier was wrong. Every prompt change ships with the before/after eval table in the commit message.

## Design

- **Answer prompt** (`src/zchut/answer/prompt.py`): add explicit output rules: plain text only (no `**`, `#`, bullets, tables); at most 6 lines before the sources block; numbers and dates as digits; no legal disclaimer boilerplate (the escalation text carries the "not legal advice" line once); if the passages do not contain the asked fact, say so in one sentence and then give what they do say. A post-processor `clean_answer()` strips any Markdown markers the model still emits and collapses blank lines; counted in the record as `markdown_stripped: n`.
- **Telegram-safe rendering**: `render_html()` produces Telegram HTML (escaped text, `<a href>` for sources, `<b>` for the sources header) from the same answer structure; the CLI keeps plain text. Both come from one `Answer` object so they cannot drift.
- **Verifier labels**: a new file `eval/verifier_labels.json` holding, per question id, the owner's label `supported` / `unsupported` with a one-line reason, seeded from the six disagreements of 2026-10-13 after reading the draft against the chunks (the owner labels; the developer prepares the six drafts and chunks in a scratch file for review). `zchut eval` reports, per verifier, **verifier accuracy** against these labels when labels exist (null otherwise).
- **Verifier prompt**: clarify what counts as unsupported (a claim with no passage that states it, a number that differs from the passage) and what does not (paraphrase, omission, a cautious "it depends"). Changed only if the labels show the Haiku verifier over-rejecting; the Jev criteria text gets the same wording.
- **Typos**: the answer prompt asks for a final proofread; no code can check Hebrew spelling, so the eval gains a cheap `markdown_free` metric (fraction of answers with no Markdown markers before cleaning) and the owner spot-reads five answers per eval.
- **Record**: `answer.markdown_stripped`, `eval.markdown_free`, `eval.verifier_accuracy`.

## Acceptance criteria

1. `clean_answer` removes `**`, leading `#`/`-`/`*` markers and collapses blank lines, leaves `[n]` citations intact, and the record counts what it removed.
2. `render_html` escapes `<`, `>`, `&` in the text, renders each source as an `<a href>` with its date, and the plain-text CLI output is unchanged for the fixture question.
3. The answer prompt contains the plain-text, 6-line and "say what is missing" rules, and the fake-model dry run output has no Markdown markers.
4. With `eval/verifier_labels.json` present, the eval table has a `verifier accuracy` column per verifier computed against the labels; without the file the column is `—`.
5. The eval reports `markdown_free` per tier.
6. `pytest` passes; every criterion has a test whose name quotes it.

## Tasks

1. Prompt rules, `clean_answer`, `render_html`, record fields, tests (criteria 1–3, 5).
2. Verifier labels file (schema, loader), eval column (criterion 4), scratch review file for the owner with the six disagreement drafts and their chunks, verifier prompt wording.
3. README "Prompt rules" paragraph and the before/after table once the owner labels; roadmap closeout.
