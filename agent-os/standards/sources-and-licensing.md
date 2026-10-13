# Sources and licensing

- Only public pages: kolzchut.org.il (text under Creative Commons BY-SA 3.0) and gov.il (government content, public). No logged-in pages, no PDFs behind forms, no third-party sites, no scraping of search results.
- Respect robots.txt and rate limits: check robots.txt once per host before fetching, honour `Disallow`, send a descriptive User-Agent with a contact address, at most one request per second per host, and cache raw HTML under `data/raw/` so a page is fetched once per ingest.
- Every chunk carries `url`, `title`, `section` (the heading path), `fetched_at` (ISO date), `license` (`CC-BY-SA-3.0` for kolzchut, `gov.il` for government pages) and `last_updated` when the page states it. A chunk without a URL is a bug.
- Attribution: answers that use kolzchut content end with the source list and the CC BY-SA attribution line; the README and any public demo state the license and link to the source pages.
- The index is derivative of CC BY-SA text: if it is ever published, it is published under the same license with the attribution file.
- Personal data: the user's question is used for the session only; run records store a hash and the retrieved chunk ids, never the question text, never names, ID numbers or anything the user typed about themselves. Conversation memory (v3) expires with the session.
- Hebrew-first output: answers, refusals and citation lines are Hebrew; English only for URLs, model names and code. Dates in `D.M.YYYY`.
- The assistant explains public rules; it does not give legal advice, and every answer that touches a decision says so in one line and points to the office.
