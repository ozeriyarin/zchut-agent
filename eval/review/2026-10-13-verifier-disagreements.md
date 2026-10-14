# Verifier disagreements, eval of 2026-10-13 (tier haiku)

Source: `runs/2026-10-13-214311-eval-haiku-haiku-report.json` and
`runs/2026-10-13-214311-eval-haiku-jev-report.json`, rows with `verified: false`.
Six ids were rejected by one verifier only; q11 and q15 were rejected by both.

The reports do not keep draft texts, so the drafts and their chunks are not here. To label,
rerun each question with `zchut ask "<question>" --tier haiku --no-verify` (a rejected draft is
replaced by the escalation text, so only `--no-verify` prints it; the new draft may differ from
the eval's), read it against its sources, fill `label:` (supported / unsupported) and a one-line
`reason:`, and copy both into `eval/verifier_labels.json`.

## q04

- question: התפטרתי מהעבודה, האם אקבל דמי אבטלה?
- rejected by: jev
- expected urls:
  - https://www.kolzchut.org.il/he/דמי_אבטלה
- label:supported 
- reason:שכירים תושבי ישראל שפוטרו, התפטרו או הוצאו לחל"ת (למינימום הימים הנדרשים).

## q10

- question: מה קורה אם לא מתייצבים בלשכת התעסוקה?
- rejected by: haiku
- expected urls:
  - https://www.kolzchut.org.il/he/דמי_אבטלה
- label:unsupported
- reason:לא מצויין מה קורה אם לא מתייצבים 

## q11

- question: כמה המעסיק חייב להפריש לפנסיה?
- rejected by: both
- expected urls:
  - https://www.kolzchut.org.il/he/פנסיה_וחיסכון_ארוך_טווח_לשכירים
- label:unsupported
- reason: that a main topic page with almost no details  , includes lots of redirect links under the same url

## q13

- question: אחרי כמה חודשי עבודה מתחילות הפרשות לפנסיה?
- rejected by: jev
- expected urls:
  - https://www.kolzchut.org.il/he/פנסיה_וחיסכון_ארוך_טווח_לשכירים
- label:unsupported
- reason: that a main topic page with almost no details , includes lots of redirect links under the same url

## q15

- question: מה קורה אם לא בחרתי קרן פנסיה?
- rejected by: both
- expected urls:
  - https://www.kolzchut.org.il/he/פנסיה_וחיסכון_ארוך_טווח_לשכירים
- label:unsupported
- reason: that a main topic page with almost no details , includes lots of redirect links under the same url

## q17

- question: איך בודקים אם המעסיק הפריש כספים לפנסיה?
- rejected by: haiku
- expected urls:
  - https://www.kolzchut.org.il/he/פנסיה_וחיסכון_ארוך_טווח_לשכירים
- label:unsupported
- reason: that a main topic page with almost no details , includes lots of redirect links under the same url

## q18

- question: איך מקבלים החזר מס הכנסה לשכירים?
- rejected by: haiku
- expected urls:
  - https://www.gov.il/he/service/income-tax-refund-for-employees
  - https://www.kolzchut.org.il/he/החזר_מס_הכנסה
- label:supported
- reason: https://www.gov.il/he/service/income-tax-refund-for-employees - 404 , but the other link is detailed 

## q23

- question: תוך כמה זמן מתקבל החזר המס?
- rejected by: haiku
- expected urls:
  - https://www.gov.il/he/service/income-tax-refund-for-employees
  - https://www.kolzchut.org.il/he/החזר_מס_הכנסה
- label:supported
- reason: https://www.gov.il/he/service/income-tax-refund-for-employees - 404 , but the other link is detailed 
