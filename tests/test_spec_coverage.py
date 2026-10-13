"""Spec coverage: every acceptance criterion in agent-os/specs has a test whose name quotes it.

Same rule as morning-agent's src/spec-coverage.test.ts, in pytest: a criterion is covered when the
string `criterion <n>: "<criterion text>"` appears in a test file under tests/ (docstring or id).
A spec whose code is not written yet is listed in UNIMPLEMENTED and its row is xfail(strict=True),
so the row starts failing loudly the moment the tests exist and the marker must then be removed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "agent-os" / "specs"
TESTS = ROOT / "tests"

# (spec folder, prefix used in the quoting string, number of criteria)
SPEC_ROWS: list[tuple[str, str, int]] = [
    ("2026-10-13-zchut-v1-ingest-ask", "", 10),
    ("2026-10-13-zchut-v2-conversation", "", 8),
    ("2026-10-13-zchut-v3-full-corpus", "", 8),
    ("2026-10-13-zchut-v4a-prompt-tuning", "", 6),
    ("2026-10-13-zchut-v4-telegram", "", 8),
]
UNIMPLEMENTED: set[str] = set()


def criteria(spec: str) -> list[tuple[str, str]]:
    text = (SPECS / spec / "spec.md").read_text(encoding="utf-8")
    section = text.split("## Acceptance criteria", 1)[1].split("\n## ", 1)[0]
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^(\d+)\. (.+?)\r?$", section, re.M)]


def all_test_sources() -> str:
    files = sorted(p for p in TESTS.rglob("test_*.py"))
    return "\n".join(p.read_text(encoding="utf-8").replace("\\", "") for p in files)


def _row(spec: str, prefix: str, count: int) -> object:
    xfail = pytest.mark.xfail(strict=True, reason="spec not implemented yet")
    marks = [xfail] if spec in UNIMPLEMENTED else []
    return pytest.param(spec, prefix, count, id=spec, marks=marks)


@pytest.mark.parametrize(("spec", "prefix", "count"), [_row(*row) for row in SPEC_ROWS])
def test_every_acceptance_criterion_has_a_test_whose_name_quotes_it(
    spec: str, prefix: str, count: int
) -> None:
    found = criteria(spec)
    assert [n for n, _ in found] == [str(i) for i in range(1, count + 1)]
    sources = all_test_sources()
    for n, text in found:
        needle = f'{prefix}criterion {n}: "{text}"'
        assert needle in sources, f"{spec} criterion {n} has no quoting test"


def test_criterion_10_pytest_passes_every_criterion_quoted() -> None:
    """criterion 10: "`pytest` passes; every criterion above has a test whose name quotes it." """
    assert len(criteria("2026-10-13-zchut-v1-ingest-ask")) == 10


def test_criterion_8_v2_pytest_passes_every_criterion_quoted() -> None:
    """criterion 8: "`pytest` passes; every criterion has a test whose name quotes it." """
    assert len(criteria("2026-10-13-zchut-v2-conversation")) == 8


def test_criterion_8_v3_pytest_passes_every_criterion_quoted() -> None:
    """criterion 8: "`pytest` passes; every criterion has a test whose name quotes it." """
    assert len(criteria("2026-10-13-zchut-v3-full-corpus")) == 8


def test_criterion_6_v4a_pytest_passes_every_criterion_quoted() -> None:
    """criterion 6: "`pytest` passes; every criterion has a test whose name quotes it." """
    assert len(criteria("2026-10-13-zchut-v4a-prompt-tuning")) == 6
