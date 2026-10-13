from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel

from zchut.types import FetchedPage


class FetchError(Exception):
    pass


class Fetcher(Protocol):
    def fetch_page(self, url: str) -> FetchedPage: ...


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
            self._meta[meta.url] = meta

    def urls(self) -> list[str]:
        return list(self._meta)

    def fetch_page(self, url: str) -> FetchedPage:
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
        self._user_agent = user_agent
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}

    def fetch_page(self, url: str) -> FetchedPage:
        if not self._robots_for(url).can_fetch(self._user_agent, url):
            raise FetchError(f"robots.txt disallows {url}")
        response = self._get(url)
        if response.status_code != 200:
            raise FetchError(f"status {response.status_code} for {url}")
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        (self._raw_dir / f"{name}.html").write_text(response.text, encoding="utf-8")
        return FetchedPage(url=url, html=response.text, fetched_at=date.today())

    def _robots_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        if parts.netloc not in self._robots:
            parser = RobotFileParser()
            response = self._get(f"{parts.scheme}://{parts.netloc}/robots.txt")
            parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            self._robots[parts.netloc] = parser
        return self._robots[parts.netloc]

    def _get(self, url: str) -> httpx.Response:
        host = urlsplit(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            wait = self._min_interval_s - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()
        try:
            return self._client.get(url, headers={"User-Agent": self._user_agent})
        except httpx.HTTPError as exc:
            raise FetchError(f"request to {host} failed: {exc}") from exc
