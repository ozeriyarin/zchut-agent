# Spec: zchut v5 — MCP server over the corpus, weekly re-ingest routine

Status: shaped 2026-10-15. Builds on v1/v3/v4. Standards applied: python-style, secrets-and-config, sources-and-licensing, testing. Shipped 2026-10-15, commit 0fca767.

## Goal

Any MCP client (Claude Code, Claude Desktop, another agent) can search the כל זכות corpus and fetch a page's chunks through two tools, with the licence attribution carried in every result. The corpus stays current without the owner: a weekly routine re-plans the crawl, fetches only changed pages, and posts a diff report.

## Design

- **MCP server** `src/zchut/mcp/server.py` using the official `mcp` Python SDK (`FastMCP`), transport stdio (`zchut mcp serve`), tools:
  - `search(query: str, k: int = 5, topic: str | None = None)` → list of `{ title, section, url, last_updated, score, text, license }`, vector search over the configured store (Qdrant or Chroma per config); `topic` filters by a metadata keyword list (`config.mcp.topics`: unemployment, pension, tax, …) mapped to title-path substrings. No model call.
  - `fetch(url: str)` → the page's chunks in order with metadata, or an error for an unknown URL. No network to the site: fetch means "from the index".
  - Every result carries `license` and `attribution` ("התוכן מאתר כל-זכות … CC BY-SA 3.0") and the server's instructions text tells clients to cite URLs. Embedding the query uses the Voyage key from `.env`; a missing key is a clear startup error.
  - Resource `zchut://stats` → `{ chunks, articles, last_crawl, skipped }` from the store count and manifest.
- **Security**: read-only, no arguments reach the shell or the filesystem beyond the configured index; `k` capped at 20; query length capped at 500 chars; results are data (text from the corpus), and the tool descriptions say so.
- **Client config**: README section with the `claude mcp add` command and a Claude Desktop JSON snippet pointing at `.venv\Scripts\zchut.exe mcp serve`.
- **Weekly re-ingest** `zchut crawl run` is already incremental; v5 adds `zchut crawl report` that reads the last two crawl records and the manifest and prints a Hebrew diff: new, changed, gone, skipped counts, the ten most recently updated titles (titles are public page names, fine to print), elapsed, tokens, cost. A Claude Code cloud routine "zchut weekly re-ingest" (owner creates it with the `schedule` skill; prompt text in README) runs Sunday 03:00 UTC: `npm`-free, `pip install -e .`, `zchut crawl run --max-minutes 50 --max-usd 0.30` against Qdrant (store backend from config), then `zchut crawl report`, and posts the report to the owner's Telegram chat via `zchut notify "<text>"` (new tiny command using the bot token; allowed chat id only). Secrets as environment variables of the routine's environment; allow list: kolzchut.org.il, api.voyageai.com, the Qdrant host, api.telegram.org, pypi.org, files.pythonhosted.org, github.com.
- **Record**: crawl records gain `report_posted: bool`; MCP calls are not recorded (no model, no cost) beyond a counter log line.

## Acceptance criteria

1. `search` over the fixture index returns up to `k` results with title, url, score, text and license, honours the topic filter, caps `k` at 20 and rejects a query over 500 chars.
2. `fetch` returns a known page's chunks in section order with metadata and an error for an unknown URL, without any network call.
3. The server exposes the two tools and the `zchut://stats` resource with the documented schemas (checked through the MCP SDK's in-process client), and every result carries the attribution line.
4. `zchut crawl report` prints the diff between the last two crawl records and the manifest (fixtures: two records and a manifest), including counts and the most recently updated titles.
5. `zchut notify "<text>"` sends one Telegram message to the allowed chat id with a stubbed transport and refuses when no allowed id is configured.
6. `pytest` passes; every criterion has a test whose name quotes it.

## Tasks

1. `src/zchut/mcp/server.py`, `zchut mcp serve`, `mcp` dependency, topic filter config, stats resource.
2. `zchut crawl report`, `zchut notify`, record field.
3. Tests (criteria 1–5), README "MCP server" and "Weekly re-ingest routine" sections with the routine prompt, roadmap closeout.
