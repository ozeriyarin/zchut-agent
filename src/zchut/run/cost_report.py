from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from zchut.run.records import RunRecordSink


@dataclass
class Tally:
    count: int = 0
    cost_usd: float = 0.0

    def add(self, cost_usd: float) -> None:
        self.count += 1
        self.cost_usd += cost_usd


@dataclass
class CostSummary:
    since: datetime
    by_command: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    by_model: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    skipped: list[str] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return sum(t.cost_usd for t in self.by_command.values())


def summarize_runs(sink: RunRecordSink, since: datetime) -> CostSummary:
    records, skipped = sink.read(since)
    summary = CostSummary(since, skipped=skipped)
    for record in records:
        summary.by_command[record.command].add(record.total_cost_usd)
        for call in record.calls:
            summary.by_model[call.model].add(call.cost_usd)
    return summary


def format_cost(summary: CostSummary) -> str:
    since = summary.since
    lines = [f"עלויות מאז {since.day}.{since.month}.{since.year}:", "לפי פקודה:"]
    lines += [
        f"  {k}: {t.count} ריצות, {t.cost_usd:.6f}$" for k, t in sorted(summary.by_command.items())
    ]
    lines.append("לפי מודל:")
    lines += [
        f"  {k}: {t.count} קריאות, {t.cost_usd:.6f}$" for k, t in sorted(summary.by_model.items())
    ]
    lines.append(f'סה"כ: {summary.total_usd:.6f}$')
    if summary.skipped:
        lines.append(f"קבצים שלא נקראו: {', '.join(summary.skipped)}")
    return "\n".join(lines)
