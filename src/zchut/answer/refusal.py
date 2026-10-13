from __future__ import annotations

from collections.abc import Sequence

from zchut.types import Hit

REFUSAL_SENTENCE = "לא מצאתי מקור אמין מספיק כדי לענות על זה. מומלץ לפנות ישירות למשרד הרלוונטי."


def should_refuse(hits: Sequence[Hit], min_score: float) -> bool:
    return not hits or max(h.score for h in hits) < min_score
