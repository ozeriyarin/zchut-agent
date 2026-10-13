from __future__ import annotations


def estimate_tokens(text: str) -> int:
    # Hebrew runs about two characters per token; overestimating keeps the guard conservative.
    return len(text) // 2 + 1
