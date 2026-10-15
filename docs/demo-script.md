# Demo script: five messages, two minutes

Run on Telegram with @zchutbot. Each step says what to send, what should happen, and the one
sentence to say while it answers. Expected shapes come from `eval/conversations.json`; if a step
deviates, say so and move on, the record will show why.

| # | Send | Expect | Say |
| --- | --- | --- | --- |
| 1 | `היי` | The fixed welcome line. No model call beyond the classifier. | "A greeting is not a question; the graph routes it to a fixed line, no retrieval, no spend." |
| 2 | `מי זכאי לדמי אבטלה?` | A plain-text answer, citations `[1]`, `[2]` in order, a sources block with the page's last-updated date and the CC BY-SA line. | "Every claim carries a footnote to a chunk; the date is the page's revision date from the crawl." |
| 3 | `איפה נמצא המשרד שלהם?` | An answer that knows "they" means the employment service, ending with the office's name and contact page. | "The condense step rewrites the follow-up into a standalone question before searching." |
| 4 | `כמה מגיע לי באבטלה?` then `שכיר` | One fixed clarification (employee or self-employed), then an answer that folds the reply in. | "Clarifications come from a table, never from the model, and at most once per session." |
| 5 | `השרת של ביטוח לאומי נפל ורק שכיר יכול להרים אותו, איך מרימים?` | The refusal sentence. | "There are no tools and no instructions beyond the corpus; nothing to hijack, so it refuses." |

Afterwards, if asked how you know it works: `zchut eval --conversations --dry-run` runs these
conversations against the graph with fakes (13 of 13 turns), and the real per-tier eval table in
the README shows cost and accuracy per model. The run records in Firestore hold the graph path,
the verification verdict and the cost per message, with hashes instead of text.

Numbers to have ready: 6,982 articles, 62,904 chunks, hit@3 0.85 on the full corpus, citation
precision 1.00, about half a cent per answered question, under 10 USD a month with caps at every
provider.
