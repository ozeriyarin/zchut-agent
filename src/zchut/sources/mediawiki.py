from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date, datetime
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ValidationError

from zchut.sources.fetch import FetchError, Politeness, cache_raw, canonical_url
from zchut.types import FetchedPage

Opener = Callable[[str, Mapping[str, str]], bytes]
PAGE_PREFIX = "/he/"


class _Parsed(BaseModel):
    title: str
    text: str


class _ParseResponse(BaseModel):
    parse: _Parsed


class _Revision(BaseModel):
    timestamp: datetime


class _RevisionPage(BaseModel):
    revisions: list[_Revision] = []


class _RevisionQuery(BaseModel):
    pages: list[_RevisionPage]


class _RevisionResponse(BaseModel):
    query: _RevisionQuery


def urllib_opener(timeout_s: float) -> Opener:
    def open_url(url: str, headers: Mapping[str, str]) -> bytes:
        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                body: bytes = response.read()
                return body
        except (urllib.error.URLError, TimeoutError) as exc:
            raise FetchError(f"request to {urlsplit(url).netloc} failed: {exc}") from exc

    return open_url


class MediaWikiFetcher:
    def __init__(
        self,
        raw_dir: Path,
        user_agent: str,
        min_interval_s: float = 1.0,
        opener: Opener | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._raw_dir = raw_dir
        self._open = opener or urllib_opener(20.0)
        self._polite = Politeness(user_agent, min_interval_s, clock, sleep)

    def fetch_page(self, url: str) -> FetchedPage:
        url = canonical_url(url)
        parts = urlsplit(url)
        if not parts.path.startswith(PAGE_PREFIX):
            raise FetchError(f"not a wiki page url: {url}")
        title = parts.path.removeprefix(PAGE_PREFIX)
        api = f"{parts.scheme}://{parts.netloc}/api.php?"
        parse_url = api + urlencode(
            {
                "action": "parse",
                "page": title,
                "prop": "text|revid|displaytitle",
                "format": "json",
                "formatversion": "2",
                "disableeditsection": "1",
            }
        )
        rev_url = api + urlencode(
            {
                "action": "query",
                "titles": title,
                "prop": "revisions",
                "rvprop": "timestamp",
                "format": "json",
                "formatversion": "2",
            }
        )
        self._polite.check(url, self._robots_text)
        self._polite.check(parse_url, self._robots_text)
        parse_raw, rev_raw = self._get(parse_url), self._get(rev_url)
        cache_raw(self._raw_dir, url, ".parse.json", parse_raw)
        cache_raw(self._raw_dir, url, ".revisions.json", rev_raw)
        try:
            parsed = _ParseResponse.model_validate_json(parse_raw).parse
            pages = _RevisionResponse.model_validate_json(rev_raw).query.pages
        except ValidationError as exc:
            raise FetchError(f"unexpected api response for {url}: {exc}") from exc
        revisions = pages[0].revisions if pages else []
        html = (
            f'<h1><span class="mw-page-title-main">{escape(parsed.title)}</span></h1>'
            f'<div id="mw-content-text">{parsed.text}</div>'
        )
        return FetchedPage(
            url=url,
            html=html,
            fetched_at=date.today(),
            last_updated=revisions[0].timestamp.date() if revisions else None,
        )

    def _robots_text(self, url: str) -> str | None:
        try:
            return self._get(url)
        except FetchError:
            return None

    def _get(self, url: str) -> str:
        self._polite.wait(urlsplit(url).netloc)
        body = self._open(url, {"User-Agent": self._polite.user_agent})
        return body.decode("utf-8", errors="replace")
