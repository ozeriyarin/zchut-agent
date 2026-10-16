# Spec: zchut v4b — conversation quality: condense, fixed clarifications, replies, citations

Status: shaped 2026-10-15 from the first live Telegram session (5 exchanges, 2 wrongly refused, 1 derailed by a generated clarification). Builds on v2/v4. Standards applied: python-style, ai-calls, testing. Shipped 2026-10-15, commit afd2eef (revision 2); the v4c addendum shipped 2026-10-15, commit 6fe4af1 (revision 3).

## Goal

A multi-turn chat behaves like a clerk who remembers the last thing you said: follow-ups resolve against the previous turns, a reply to the bot's own question is treated as a reply, clarifications are asked rarely and in fixed, correct Hebrew, and the search sees one clean standalone question. Citations are numbered in display order.

## Design

- **Condense step** (new graph node `condense`, before `classify`): when `history` is non-empty, one Haiku call with a JSON schema rewrites the latest message plus the last turns into `{ standalone_question: str, is_reply_to_clarification: bool, is_question: bool }`. Rules in the prompt: keep Hebrew, resolve pronouns and ellipsis ("their office" → the office named in the previous answer), fold a pending clarification reply into the question, output the message unchanged when it already stands alone. With empty history the step is skipped and `standalone_question = message`. The standalone question is what `retrieve`, `answer` and `verify` use; the record stores only its hash.
- **Replies and non-questions**: if `is_reply_to_clarification` and the session has a pending clarification, the graph resumes with the folded question (no second classification of the reply as a fresh question). If `is_question` is false and nothing is pending (a reaction such as "ההעפלה?" or "מה קשור?"), the bot replies with a fixed short Hebrew line asking what it can look up, makes no retrieval or answer call, and keeps the history. Counted in the record as `bot_event: 'nudge'`.
- **Clarification policy**: `classify` returns `missing_fact` as an enum from `config/clarifications.json`, each entry `{ key, question_he, applies_to_topics }`; the clarification text sent to the user is the fixed `question_he`, never model text. Clarify only when the fact changes the answer (amount or duration questions) and the topic matches; never for eligibility, "how to", "where", "what documents" questions (the classifier prompt lists these), and at most once per session (`Session.clarified_once`).
- **Threshold on the standalone question**: retrieval scores the condensed question; the refusal threshold stays 0.57. A clarification reply is never searched raw.
- **Office questions**: when the condensed question asks where or how to contact an office and the topic is known, the graph goes to `escalate` with the office entry (name, what it handles, contact page) as a normal answer, not a refusal; `escalated` stays false and the record marks `office_lookup: true`.
- **Citations**: `resolve` renumbers citations in order of first appearance in the answer (`[3]` then `[4]` become `[1]` and `[2]`) and the sources block follows the new numbers; invented numbers still dropped.
- **Multi-turn eval**: `eval/conversations.json` with the five live exchanges (owner-paraphrased, no personal data) and expectations per turn: `expect` ∈ `answer | clarify:<key> | nudge | office | refuse`, optional `expected_urls`. `zchut eval --conversations` runs them through the graph with the fakes (dry run) or the real tiers and reports turn accuracy and per-turn outcomes; the dry-run version runs in the test suite.
- **Cost**: the condense call adds roughly 300 input tokens per follow-up on Haiku; per-message default cap unchanged at 0.08.

## Acceptance criteria

1. With history, `condense` returns a standalone question that resolves "איפה המשרד שלהם" after an unemployment answer into a question naming the employment service, and leaves an already standalone question unchanged (fake condenser in tests, prompt text asserted).
2. A reply to a pending clarification is folded into the question and answered without a second clarification; a non-question with nothing pending gets the fixed nudge line, no retrieval and no answer call.
3. Clarifications are the fixed `question_he` from `config/clarifications.json`; "מי זכאי לדמי אבטלה?" and "איך נרשמים" never trigger one; "כמה מגיע לי באבטלה?" triggers at most one per session.
4. An office question with a known topic returns the office entry text with `office_lookup: true`, not the refusal sentence.
5. Citations in the final answer are renumbered in order of first appearance and the sources block matches; a dropped invented number does not consume a display number.
6. `zchut eval --conversations --dry-run` runs the five live conversations with the expected outcomes per turn and reports turn accuracy; the suite asserts 100% on the fakes.
7. The record stores the standalone question as a hash only, never its text, together with `bot_event` and `office_lookup` (redaction test).
8. `pytest` passes; every criterion has a test whose name quotes it.
9. A greeting or reaction on the first turn gets the fixed welcome line with no retrieval or answer call.
10. An office question answered from the corpus still ends with the office's name and contact page.
11. Characters outside Hebrew, Latin, digits and punctuation are removed from the answer and counted in the record.
12. Any question about rights, benefits or public services in Israel is in scope regardless of topic; only clearly unrelated requests are out of scope.
13. When the verifier marks some claims unsupported, only the sentences carrying them are removed, a Hebrew omission line is appended, and the record counts the trimmed sentences; escalation happens only when nothing substantive remains.
14. The verifier prompt treats a paraphrase of a sourced fact as supported.

## Tasks

1. `condense` node + prompt + schema + fake; `config/clarifications.json`; classify prompt and enum; session `clarified_once`; office lookup branch; citation renumbering.
2. Bot webhook and CLI `chat` wired to the new flow; records.
3. `eval/conversations.json` (5 conversations), `zchut eval --conversations`, tests for criteria 1–7, README "Conversations" paragraph, roadmap closeout.

## Addendum v4c (2026-10-15)

From the second live Telegram session. Criteria 9–11 above; the spec-coverage test reads only the acceptance criteria list, so they are numbered there.

- **Greeting** (criterion 9): the classifier schema gains `intent: question | greeting | reaction`. A greeting or reaction with nothing pending goes to `nudge`: greeting gets the fixed welcome line, reaction the existing nudge line; no retrieval, no answer call, `bot_event: 'nudge'`. The fake classifier matches a short word list (היי, שלום, הי, בוקר טוב, תודה, אוקי).
- **Office line on corpus answers** (criterion 10): when the classifier marks `office_lookup` or the condensed question asks where or how to contact an office, and the graph still takes the retrieval path, `final` appends the office line (name and contact page from `config/offices.json` for the topic) after the answer text and before the sources. Routing is unchanged.
- **Foreign-script scrub** (criterion 11): `clean_answer` drops every run of letters or marks outside the Hebrew and Latin scripts (Hangul, CJK, Cyrillic tokens) and the record counts the runs in `scrubbed`.
- **Eval**: a sixth conversation opens with a greeting (`nudge`); an `office` turn is correct only when the final text holds the office line.
