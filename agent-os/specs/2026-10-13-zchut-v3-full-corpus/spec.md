# Spec: zchut v3 — full corpus: every כל זכות article, incremental and resumable

Status: shaped 2026-10-13. Builds on v1. Standards applied: python-style, secrets-and-config, testing, sources-and-licensing. Shipped 2026-10-13, commit 097718c.

## Goal

The corpus becomes the whole of כל זכות (about 7,370 articles, 8.4 million words) instead of six hand-picked pages, and stays current: a later run fetches and embeds only articles whose revision changed. The crawl respects the site's crawl delay, survives interruption, and never re-embeds an unchanged page. Everything runs on the owner's machine at the free-tier pace; the rate maths is in the record so the owner can decide whether to pay for speed.

## Design

- **Enumeration** (`src/zchut/sources/catalog.py`): `list_articles()` pages through `action=query&list=allpages&apnamespace=0&apfilterredir=nonredirects&aplimit=500` with `apcontinue`, over the same urllib opener and politeness as v1. Titles that start with a configured skip prefix (`config.crawl.skip_prefixes`, e.g. `תבנית:`, `קטגוריה:`, disambiguation markers) are dropped. Result: list of `{ title, url }`.
- **Revisions in bulk**: `latest_revisions(titles)` calls `action=query&prop=revisions&rvprop=ids|timestamp&titles=<up to 50 joined by |>` → `{ title: (revid, timestamp) }`; 50 titles per request, so 7,370 titles cost about 150 requests (12 minutes at the crawl delay).
- **Manifest** `data/manifest.json` (gitignored, local state): `{ "version": 1, "articles": { "<url>": { "revid", "last_updated", "fetched_at", "chunks", "status": "ok" | "error", "error"?: string } } }`. The manifest is the source of truth for "what is indexed"; the store is rebuilt from it if ever lost.
- **Plan**: `crawl plan` = enumerate + bulk revisions, then diff against the manifest → `new`, `changed` (revid differs), `unchanged`, `gone` (in manifest, not in the listing). Prints the counts and the time estimate: `(new + changed) × crawl_delay` for fetching and `estimated_tokens / embed.max_batch_tokens × embed.min_interval_s` for embedding.
- **Run**: `crawl run [--limit N] [--max-usd X] [--max-minutes M]` processes `new` + `changed` in title order, one article at a time: fetch (v1 `MediaWikiFetcher`), parse, chunk, embed, `store.replace(url, …)`, then write the manifest entry **after** the replace succeeds (so a crash never leaves a page marked done but not stored). A parse or fetch error records `status: error` and continues; `BudgetExceeded` or `--max-minutes` stops cleanly with the manifest consistent. `gone` articles are deleted from the store and removed from the manifest. Re-running continues where it stopped.
- **Records**: `runs/<ts>-crawl.json` with counts (planned, fetched, embedded, errors, skipped, deleted), total chunks in store, elapsed, embed tokens and cost, and the rate estimate used. No article text.
- **Store**: Chroma stays local for v3 (about 100 k chunks × 1024 floats ≈ 400 MB on disk; fine). `count()` is reported. Qdrant arrives with Telegram (v4) behind the same interface.
- **Eval**: unchanged, but `zchut eval --retrieval-only` after a full crawl is the acceptance check that the six-page results did not regress (hit@3 ≥ 0.90 on the same 30 questions); the README records the before/after.
- **Pace**: at the free tier the first full crawl is about 10–12 hours of wall time (fetch at 5 s per article, embedding paced at 20 s per request) and costs nothing on Voyage's free tokens; the owner runs it in chunks with `--max-minutes` or overnight. README states this and the paid-tier alternative (add a card to Voyage to lift the rate limit; tokens stay free up to 200 M).

## Acceptance criteria

1. `list_articles` pages through `allpages` with `apcontinue` (two-page fixture), keeps only namespace-0 non-redirect titles, drops configured skip prefixes, and builds canonical unencoded URLs.
2. `latest_revisions` batches 50 titles per request (fixture of 120 titles → 3 requests) and returns revid and timestamp per title.
3. `crawl plan` against a fixture manifest classifies articles as new, changed, unchanged and gone, and prints the counts and the time estimate from the configured delays.
4. `crawl run --limit 2` fetches, chunks, embeds and stores exactly two planned articles, writes their manifest entries only after the store replace, and a second run continues with the next articles; an unchanged article is never fetched or embedded.
5. A fetch error on one article records `status: error` in the manifest and the run continues; a `gone` article is removed from the store and the manifest.
6. `--max-minutes` stops between articles with the manifest consistent (an injected clock), and `--max-usd` aborts before the embed call that would exceed it.
7. The crawl record stores counts, elapsed, tokens and cost and no article text or titles (redaction test).
8. `pytest` passes; every criterion has a test whose name quotes it.

## Tasks

1. `src/zchut/sources/catalog.py` (enumeration, bulk revisions), `src/zchut/index/manifest.py`, config `crawl` block.
2. `src/zchut/index/crawl.py` (plan, run, stop conditions, gone handling), CLI `crawl plan|run`, crawl record.
3. Fixtures (allpages pages, revisions, manifest), tests for criteria 1–7, README section "Full corpus" with the pace maths and the resume instructions, roadmap closeout (Telegram becomes v4).
