from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, Field, ValidationError

from zchut.sources.fetch import FetchError
from zchut.sources.mediawiki import PAGE_PREFIX

ApiGet = Callable[[str], str]


@dataclass(frozen=True)
class Article:
    title: str
    url: str


@dataclass(frozen=True)
class Revision:
    revid: int
    timestamp: datetime


class _ListedPage(BaseModel):
    ns: int
    title: str


class _AllPages(BaseModel):
    allpages: list[_ListedPage] = []


class _Continue(BaseModel):
    apcontinue: str


class _AllPagesResponse(BaseModel):
    query: _AllPages
    next: _Continue | None = Field(default=None, alias="continue")


class _Rev(BaseModel):
    revid: int
    timestamp: datetime


class _RevPage(BaseModel):
    title: str
    missing: bool = False
    revisions: list[_Rev] = []


class _RevQuery(BaseModel):
    pages: list[_RevPage] = []


class _RevResponse(BaseModel):
    query: _RevQuery


def article_url(api_url: str, title: str) -> str:
    parts = urlsplit(api_url)
    return f"{parts.scheme}://{parts.netloc}{PAGE_PREFIX}{title.replace(' ', '_')}"


def _query(api_url: str, params: dict[str, str]) -> str:
    query = {"action": "query", **params, "format": "json", "formatversion": "2"}
    return f"{api_url}?{urlencode(query)}"


def list_articles(
    get: ApiGet, api_url: str, skip_prefixes: Sequence[str], batch: int = 500
) -> list[Article]:
    articles: list[Article] = []
    params = {
        "list": "allpages",
        "apnamespace": "0",
        "apfilterredir": "nonredirects",
        "aplimit": str(batch),
    }
    while True:
        try:
            response = _AllPagesResponse.model_validate_json(get(_query(api_url, params)))
        except ValidationError as exc:
            raise FetchError(f"unexpected allpages response: {exc}") from exc
        articles.extend(
            Article(page.title, article_url(api_url, page.title))
            for page in response.query.allpages
            if page.ns == 0 and not page.title.startswith(tuple(skip_prefixes))
        )
        if response.next is None:
            return articles
        params["apcontinue"] = response.next.apcontinue


def latest_revisions(
    get: ApiGet, api_url: str, titles: Sequence[str], batch: int = 50
) -> dict[str, Revision]:
    revisions: dict[str, Revision] = {}
    for start in range(0, len(titles), batch):
        params = {
            "prop": "revisions",
            "rvprop": "ids|timestamp",
            "titles": "|".join(titles[start : start + batch]),
        }
        try:
            response = _RevResponse.model_validate_json(get(_query(api_url, params)))
        except ValidationError as exc:
            raise FetchError(f"unexpected revisions response: {exc}") from exc
        for page in response.query.pages:
            if not page.missing and page.revisions:
                rev = page.revisions[0]
                revisions[page.title] = Revision(rev.revid, rev.timestamp)
    return revisions
