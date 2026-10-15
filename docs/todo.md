# Open items (2026-10-15, end of build)

Everything in scope is shipped and deployed. These are the owner-side items left.

1. **Email kolzchut** (contact form on the site) asking to allowlist the User-Agent
   `zchut-agent/0.1 (+mailto:ozeriyarin@gmail.com)` or to point at a periodic dump.
   The draft is in the chat of 2026-10-15; Cloudflare answers 403 to datacenter sources.
   When granted: uncomment the cron in `.github/workflows/reingest.yml`, add the repo secrets
   `VOYAGE_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`, `ZCHUT_TELEGRAM_BOT_TOKEN`,
   `ZCHUT_TELEGRAM_ALLOWED_CHAT_IDS`, and run the workflow once with `probe_only` off.
2. **Decide on the local weekly task** ("zchut weekly re-ingest (local)" in the desktop app,
   Sundays 09:00). Keep it as the interim refresh, or disable it and leave the corpus at the
   2026-10-15 snapshot.
3. **Connect the MCP server** to Claude Code and try one search:
   `claude mcp add zchut -- C:\zchut-agent\.venv\Scripts\zchut.exe mcp serve`
4. **Run the five-message demo** on @zchutbot once (`docs/demo-script.md`).
5. **Delete the two disabled cloud routines**: "TEMP one-off task (delete after)" and
   "zchut weekly re-ingest (disabled ...)". The API cannot delete; use the routines page.
6. **2026-10-25, DST ends**: move the three morning-agent routines one hour later in UTC
   (brief 04→05, replay 05→06, inbox 4–20→5–21).

Roadmap items v7 onward (`agent-os/product/roadmap.md`) are future work by decision, not open
items.
