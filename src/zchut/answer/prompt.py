from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from zchut.types import Hit

SYSTEM_PROMPT = (
    "אתה עוזר שמסביר זכויות מול הבירוקרטיה בישראל. "
    "ענה בעברית בלבד, בפסקאות קצרות, רק על סמך קטעי המקור הממוספרים שבהודעה. "
    "אחרי כל טענה ציין את מספר הקטע שעליו היא נשענת בסוגריים מרובעים, למשל [1] או [2]. "
    "מותר לצטט רק מספרים של קטעים שקיבלת. אל תוסיף מידע שאינו מופיע בקטעים. "
    "כתוב טקסט רגיל בלבד, בלי Markdown: בלי **, בלי #, בלי תבליטים ובלי טבלאות. "
    "כתוב לכל היותר 6 שורות. כתוב מספרים ותאריכים בספרות. "
    "אם הקטעים אינם מכילים את הפרט שנשאל, אמור זאת במשפט אחד ואז כתוב מה הם כן אומרים. "
    "אל תוסיף הסתייגות משפטית; היא מתווספת בנפרד כשצריך. "
    "אל תכתוב רשימת מקורות בסוף; היא מתווספת אוטומטית עם תאריכי העדכון. "
    "לפני שאתה מסיים, קרא שוב את התשובה ותקן שגיאות כתיב."
)


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    hits: tuple[Hit, ...]


def format_date(iso: str | None) -> str | None:
    if iso is None:
        return None
    d = date.fromisoformat(iso)
    return f"{d.day}.{d.month}.{d.year}"


def numbered_sources(hits: Sequence[Hit]) -> str:
    blocks = []
    for n, hit in enumerate(hits, start=1):
        c = hit.chunk
        updated = format_date(c.last_updated)
        lines = [f"[{n}] {c.section}", f"כתובת: {c.url}"]
        if updated:
            lines.append(f"עודכן: {updated}")
        lines.append(c.text)
        blocks.append("\n".join(lines))
    return "קטעי מקור:\n\n" + "\n\n".join(blocks)


def build_prompt(question: str, hits: Sequence[Hit]) -> Prompt:
    user = f"{numbered_sources(hits)}\n\nשאלה: {question}"
    return Prompt(system=SYSTEM_PROMPT, user=user, hits=tuple(hits))
