from __future__ import annotations

import json
import socket
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextResourceContents
from qdrant_client import QdrantClient
from typer.testing import CliRunner

from conftest import AVTALA, FIXTURES, PENSIA, TAX, make_guard
from zchut.cli import app
from zchut.config import Prices, Settings
from zchut.index.embedder import FakeEmbedder
from zchut.index.ingest import ingest
from zchut.index.manifest import Manifest, ManifestEntry
from zchut.index.store import ChromaStore, InMemoryVectorStore, PageStore, QdrantStore
from zchut.mcp.server import (
    ATTRIBUTION,
    MAX_K,
    STATS_URI,
    ServerDeps,
    create_server,
    fetch,
    search,
)
from zchut.sources.fetch import FixtureFetcher
from zchut.types import Hit

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")
T = TypeVar("T")
QUERY = "מי זכאי לדמי אבטלה"
FIELDS = {"title", "section", "url", "last_updated", "score", "text", "license", "attribution"}


class SpyStore(InMemoryVectorStore):
    def __init__(self, source: InMemoryVectorStore) -> None:
        super().__init__()
        self._by_url = source._by_url
        self.ks: list[int] = []

    def search(self, vector: Sequence[float], k: int) -> list[Hit]:
        self.ks.append(k)
        return super().search(vector, k)


def manifest() -> Manifest:
    entry = ManifestEntry(
        revid=1, last_updated=None, fetched_at="2026-10-11", chunks=0, status="ok"
    )
    skipped = entry.model_copy(update={"status": "skipped"})
    return Manifest(articles={AVTALA: entry, PENSIA: entry, "https://x/he/ריק": skipped})


def make_deps(store: PageStore, settings: Settings, priced: Prices) -> ServerDeps:
    return ServerDeps(
        FakeEmbedder(),
        store,
        make_guard(priced),
        settings.mcp.topics,
        manifest,
        lambda: "2026-10-11T03:00:00",
    )


def with_session(deps: ServerDeps, use: Callable[[ClientSession], Awaitable[T]]) -> T:
    async def main() -> T:
        async with create_connected_server_and_client_session(create_server(deps)) as session:
            return await use(session)

    return anyio.run(main)


def test_criterion_1_search_k_topic_and_caps(
    fixture_store: InMemoryVectorStore, settings: Settings, priced: Prices
) -> None:
    """criterion 1: "`search` over the fixture index returns up to `k` results with title, url, score, text and license, honours the topic filter, caps `k` at 20 and rejects a query over 500 chars." """
    store = SpyStore(fixture_store)
    deps = make_deps(store, settings, priced)
    found = search(deps, QUERY, 3, None)
    assert len(found) == 3 and found[0].url == AVTALA
    assert set(found[0].model_dump()) == FIELDS and found[0].license == "CC-BY-SA-3.0"
    assert [r.score for r in found] == sorted((r.score for r in found), reverse=True)

    for topic, url in [("unemployment", AVTALA), ("pension", PENSIA), ("tax", TAX)]:
        hits = search(deps, QUERY, 20, topic)
        assert hits and {h.url for h in hits} == {url}, topic

    store.ks.clear()
    assert len(search(deps, QUERY, 50, None)) == MAX_K and store.ks == [MAX_K]
    assert len(search(deps, QUERY, 0, None)) == 1
    calls = len(deps.guard.recorder.record.calls)
    for bad in ["א" * 501, "   "]:
        with pytest.raises(ToolError, match="500"):
            search(deps, bad, 5, None)
    with pytest.raises(ToolError, match="unemployment"):
        search(deps, QUERY, 5, "weather")
    assert len(search(deps, "א" * 500, 1, None)) == 1
    assert len(deps.guard.recorder.record.calls) == calls + 1


def _fill(store: PageStore, priced: Prices, settings: Settings, dims: int) -> None:
    fetcher = FixtureFetcher(FIXTURES)
    guard, chunk = make_guard(priced), settings.chunk
    ingest(fetcher.urls(), fetcher, FakeEmbedder(dims), store, guard, **chunk.model_dump())


def test_criterion_2_fetch_returns_page_in_order_offline(
    fixture_store: InMemoryVectorStore,
    settings: Settings,
    priced: Prices,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """criterion 2: "`fetch` returns a known page's chunks in section order with metadata and an error for an unknown URL, without any network call." """

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network call")

    deps = make_deps(fixture_store, settings, priced)
    monkeypatch.setattr(socket, "socket", no_network)
    chunks = fetch(deps, AVTALA)
    monkeypatch.undo()
    want = [c for c, _ in fixture_store._by_url[AVTALA]]
    assert [c.text for c in chunks] == [c.text for c in want] and len(chunks) == 8
    assert chunks[0].section.startswith("דמי אבטלה") and chunks[0].attribution == ATTRIBUTION
    assert {c.url for c in chunks} == {AVTALA} and {c.license for c in chunks} == {"CC-BY-SA-3.0"}
    with pytest.raises(ToolError, match="not in the index"):
        fetch(deps, "https://www.kolzchut.org.il/he/אין_דף_כזה")
    assert deps.guard.recorder.record.calls == []

    chroma = ChromaStore(tmp_path / "chroma")
    qdrant = QdrantStore(QdrantClient(":memory:"), 512)
    for store, dims in [(chroma, 1024), (qdrant, 512)]:
        _fill(store, priced, settings, dims)
        assert store.page(AVTALA) == fixture_store.page(AVTALA) == want
        assert store.page("https://www.kolzchut.org.il/he/אין_דף_כזה") == []


def test_criterion_3_mcp_client_sees_tools_resource_and_attribution(
    fixture_store: InMemoryVectorStore, settings: Settings, priced: Prices
) -> None:
    """criterion 3: "The server exposes the two tools and the `zchut://stats` resource with the documented schemas (checked through the MCP SDK's in-process client), and every result carries the attribution line." """
    deps = make_deps(fixture_store, settings, priced)

    async def use(session: ClientSession) -> dict[str, Any]:
        tools = (await session.list_tools()).tools
        resources = (await session.list_resources()).resources
        stats = (await session.read_resource(STATS_URI)).contents
        found = await session.call_tool("search", {"query": QUERY, "k": 4, "topic": "pension"})
        page = await session.call_tool("fetch", {"url": PENSIA})
        missing = await session.call_tool("fetch", {"url": "https://example.com/x"})
        return {
            "tools": tools,
            "resources": resources,
            "stats": stats,
            "found": found,
            "page": page,
            "missing": missing,
        }

    got = with_session(deps, use)
    tools = {t.name: t for t in got["tools"]}
    assert set(tools) == {"search", "fetch"}
    search_schema = tools["search"].inputSchema
    assert set(search_schema["properties"]) == {"query", "k", "topic"}
    assert search_schema["required"] == ["query"]
    assert tools["fetch"].inputSchema["required"] == ["url"]
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True and "not instructions" in tool.description
        assert tool.outputSchema is not None
    assert [str(r.uri) for r in got["resources"]] == [STATS_URI]
    [stats] = got["stats"]
    assert isinstance(stats, TextResourceContents)
    assert json.loads(stats.text) == {
        "chunks": 20,
        "articles": 2,
        "last_crawl": "2026-10-11T03:00:00",
        "skipped": 1,
    }
    for name, count in [("found", 4), ("page", 7)]:
        result = got[name]
        assert not result.isError
        rows = result.structuredContent["result"]
        assert len(rows) == count and {r["attribution"] for r in rows} == {ATTRIBUTION}
        assert {r["url"] for r in rows} == {PENSIA}
    assert got["missing"].isError
    assert "cite the url" in (create_server(deps).instructions or "").lower()


def test_mcp_serve_without_voyage_key_is_a_clear_startup_error(cli_root: Path) -> None:
    result = CliRunner().invoke(app, ["mcp", "serve"])
    assert result.exit_code == 1 and "VOYAGE_API_KEY" in result.output
