from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel

from zchut.types import FetchedPage

MAX_REDIRECTS = 3


class FetchError(Exception):
    pass


def canonical_url(url: str) -> str:
    return unquote(url)


def on_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def cache_raw(raw_dir: Path, url: str, suffix: str, text: str) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    (raw_dir / f"{name}{suffix}").write_text(text, encoding="utf-8")


class Fetcher(Protocol):
    def fetch_page(self, url: str) -> FetchedPage: ...


class FetcherRouter:
    def __init__(self, by_domain: Mapping[str, Fetcher], default: Fetcher) -> None:
        self._by_domain = by_domain
        self._default = default

    def for_url(self, url: str) -> Fetcher:
        host = urlsplit(url).hostname or ""
        matches = (f for d, f in self._by_domain.items() if on_domain(host, d))
        return next(matches, self._default)

    def fetch_page(self, url: str) -> FetchedPage:
        return self.for_url(url).fetch_page(url)


class Politeness:
    def __init__(
        self,
        user_agent: str,
        min_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}

    def check(self, url: str, load: Callable[[str], str | None]) -> None:
        parts = urlsplit(url)
        if parts.netloc not in self._robots:
            parser = RobotFileParser()
            text = load(f"{parts.scheme}://{parts.netloc}/robots.txt")
            parser.parse(text.splitlines() if text is not None else [])
            self._robots[parts.netloc] = parser
        if not self._robots[parts.netloc].can_fetch(self.user_agent, url):
            raise FetchError(f"robots.txt disallows {url}")

    def wait(self, host: str) -> None:
        robots = self._robots.get(host)
        delay = robots.crawl_delay(self.user_agent) if robots is not None else None
        interval = max(self._min_interval_s, float(delay or 0))
        last = self._last_request.get(host)
        if last is not None:
            wait = interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()


class FixtureMeta(BaseModel):
    url: str
    file: str
    fetched_at: date


class FixtureFetcher:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._meta: dict[str, FixtureMeta] = {}
        for path in sorted(directory.glob("*.meta.json")):
            meta = FixtureMeta.model_validate(json.loads(path.read_text(encoding="utf-8")))
            self._meta[canonical_url(meta.url)] = meta

    def urls(self) -> list[str]:
        return list(self._meta)

    def fetch_page(self, url: str) -> FetchedPage:
        url = canonical_url(url)
        meta = self._meta.get(url)
        if meta is None:
            raise FetchError(f"no fixture for {url}")
        try:
            html = (self._dir / meta.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise FetchError(f"cannot read fixture {meta.file}: {exc}") from exc
        return FetchedPage(url=url, html=html, fetched_at=meta.fetched_at)


class HttpxFetcher:
    def __init__(
        self,
        client: httpx.Client,
        raw_dir: Path,
        user_agent: str,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._raw_dir = raw_dir
        self._polite = Politeness(user_agent, min_interval_s, clock, sleep)

    def fetch_page(self, url: str) -> FetchedPage:
        url = canonical_url(url)
        host = urlsplit(url).netloc
        for _ in range(MAX_REDIRECTS + 1):
            self._polite.check(url, self._robots_text)
            response = self._get(url, follow_redirects=False)
            if not response.is_redirect:
                break
            url = canonical_url(str(response.url.join(response.headers["Location"])))
            if urlsplit(url).netloc != host:
                raise FetchError(f"cross-host redirect to {urlsplit(url).netloc} refused")
        else:
            raise FetchError(f"more than {MAX_REDIRECTS} redirects for {url}")
        if response.status_code != 200:
            raise FetchError(f"status {response.status_code} for {url}")
        cache_raw(self._raw_dir, url, ".html", response.text)
        return FetchedPage(url=url, html=response.text, fetched_at=date.today())

    def _robots_text(self, url: str) -> str | None:
        response = self._get(url)
        return response.text if response.status_code == 200 else None

    def _get(self, url: str, follow_redirects: bool = True) -> httpx.Response:
        host = urlsplit(url).netloc
        self._polite.wait(host)
        try:
            headers = {"User-Agent": self._polite.user_agent}
            return self._client.get(url, headers=headers, follow_redirects=follow_redirects)
        except httpx.HTTPError as exc:
            raise FetchError(f"request to {host} failed: {exc}") from exc
