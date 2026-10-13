from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from zchut.types import Hit

SYSTEM_PROMPT = (
    "אתה עוזר שמסביר זכויות מול הבירוקרטיה בישראל. "
    "ענה בעברית, בפסקאות קצרות, רק על סמך קטעי המקור הממוספרים שבהודעה. "
    "אחרי כל טענה ציין את מספר הקטע שעליו היא נשענת בסוגריים מרובעים, למשל [1] או [2]. "
    "מותר לצטט רק מספרים של קטעים שקיבלת. אל תוסיף מידע שאינו מופיע בקטעים. "
    "אם הקטעים אינם עונים על השאלה, אמור זאת במשפט אחד. "
    "אל תכתוב רשימת מקורות בסוף; היא מתווספת אוטומטית עם תאריכי העדכון. "
    "אם התשובה נוגעת להחלטה אישית, ציין בשורה אחת שאין זה ייעוץ משפטי והפנה למשרד הרלוונטי."
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
