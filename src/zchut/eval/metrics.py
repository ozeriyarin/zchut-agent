from __future__ import annotations

from collections.abc import Collection, Sequence


def hit_at_k(retrieved_urls: Sequence[str], expected_urls: Collection[str], k: int = 3) -> bool:
    return any(url in expected_urls for url in retrieved_urls[:k])


def citation_precision(
    cited: Sequence[Collection[str]], expected: Sequence[Collection[str]]
) -> float | None:
    total = sum(len(c) for c in cited)
    if total == 0:
        return None
    good = sum(len([u for u in c if u in e]) for c, e in zip(cited, expected, strict=True))
    return good / total


def rate(flags: Sequence[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0
