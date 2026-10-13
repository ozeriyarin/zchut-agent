from __future__ import annotations

from collections.abc import Callable

import httpx

BACKOFF_START_S = 20.0
BACKOFF_CAP_S = 60.0


def backoff_s(response: httpx.Response, attempt: int) -> float:
    retry_after: str = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return min(BACKOFF_START_S * 2.0**attempt, BACKOFF_CAP_S)


def send_with_backoff(
    post: Callable[[], httpx.Response], max_attempts: int, sleep: Callable[[float], None]
) -> tuple[httpx.Response, int]:
    for attempt in range(max_attempts):
        response = post()
        if response.status_code != 429 and response.status_code < 500:
            return response, attempt + 1
        if attempt + 1 < max_attempts:
            sleep(backoff_s(response, attempt))
    return response, max_attempts
