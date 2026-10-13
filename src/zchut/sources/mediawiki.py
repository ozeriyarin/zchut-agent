from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date, datetime
from html import escape
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ValidationError

from zchut.sources.fetch import (
    MAX_REDIRECTS,
    FetchError,
    Politeness,
    cache_raw,
    canonical_url,
)
from zchut.types import FetchedPage

PAGE_PREFIX = "/he/"
FORM = "application/x-www-form-urlencoded"


class Opener(Protocol):
    def __call__(
        self, url: str, headers: Mapping[str, str], data: bytes | None = None
    ) -> bytes: ...


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


class SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urlsplit(newurl)
        if req.data is not None:
            raise FetchError(f"redirect of POST to {target.netloc} refused")
        if target.scheme != "https" or target.netloc != urlsplit(req.full_url).netloc:
            raise FetchError(f"cross-host redirect to {target.netloc} refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_opener(timeout_s: float) -> Opener:
    opener = urllib.request.build_opener(SameHostRedirectHandler())

    def open_url(url: str, headers: Mapping[str, str], data: bytes | None = None) -> bytes:
        if urlsplit(url).scheme != "https":
            raise FetchError(f"refusing non-https url {url}")
        request = urllib.request.Request(url, data=data, headers=dict(headers))
        try:
            with opener.open(request, timeout=timeout_s) as response:
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
        return self.fetch_article(url, None)

    def api_get(self, url: str) -> str:
        self._polite.check(url, self._robots_text)
        return self._get(url)

    def api_post(self, url: str, params: Mapping[str, str]) -> str:
        self._polite.check(url, self._robots_text)
        body = urlencode(params)
        text = self._get(url, body.encode("utf-8"))
        cache_raw(self._raw_dir, f"{url}?{body}", ".post.json", text)
        return text

    def fetch_article(self, url: str, last_updated: date | None) -> FetchedPage:
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
        self._polite.check(url, self._robots_text)
        parse_raw = self.api_get(parse_url)
        cache_raw(self._raw_dir, url, ".parse.json", parse_raw)
        try:
            parsed = _ParseResponse.model_validate_json(parse_raw).parse
        except ValidationError as exc:
            raise FetchError(f"unexpected api response for {url}: {exc}") from exc
        if last_updated is None:
            last_updated = self._revision_date(url, api, title)
        html = (
            f'<h1><span class="mw-page-title-main">{escape(parsed.title)}</span></h1>'
            f'<div id="mw-content-text">{parsed.text}</div>'
        )
        return FetchedPage(
            url=url,
            html=html,
            fetched_at=date.today(),
            last_updated=last_updated,
        )

    def _revision_date(self, url: str, api: str, title: str) -> date | None:
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
        rev_raw = self._get(rev_url)
        cache_raw(self._raw_dir, url, ".revisions.json", rev_raw)
        try:
            pages = _RevisionResponse.model_validate_json(rev_raw).query.pages
        except ValidationError as exc:
            raise FetchError(f"unexpected api response for {url}: {exc}") from exc
        revisions = pages[0].revisions if pages else []
        return revisions[0].timestamp.date() if revisions else None

    def _robots_text(self, url: str) -> str | None:
        try:
            return self._get(url)
        except FetchError:
            return None

    def _get(self, url: str, data: bytes | None = None) -> str:
        self._polite.wait(urlsplit(url).netloc)
        headers = {"User-Agent": self._polite.user_agent}
        if data is not None:
            headers["Content-Type"] = FORM
        body = self._open(url, headers, data)
        return body.decode("utf-8", errors="replace")
