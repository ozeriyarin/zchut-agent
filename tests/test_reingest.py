from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from conftest import ROOT
from zchut import cli
from zchut.bot.telegram import TelegramClient
from zchut.cli import app

runner = CliRunner()
CRAWL = ROOT / "tests" / "fixtures" / "crawl"
LATEST = "2026-10-11-030009-crawl.json"


def stub_telegram(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    sent: list[tuple[str, dict[str, object]]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    def client(token: str, timeout_s: float) -> TelegramClient:
        return TelegramClient(httpx.Client(transport=httpx.MockTransport(answer)), token)

    monkeypatch.setattr(cli, "_telegram", client)
    return sent


def chat_ids(monkeypatch: pytest.MonkeyPatch, ids: str) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", ids)


@pytest.fixture
def crawl_root(cli_root: Path) -> Path:
    shutil.copytree(CRAWL / "records", cli_root / "runs")
    (cli_root / "data").mkdir()
    shutil.copy(CRAWL / "report-manifest.json", cli_root / "data" / "manifest.json")
    return cli_root


def test_criterion_4_crawl_report_prints_the_diff(
    crawl_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """criterion 4: "`zchut crawl report` prints the diff between the last two crawl records and the manifest (fixtures: two records and a manifest), including counts and the most recently updated titles." """
    result = runner.invoke(app, ["crawl", "report"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "2026-10-11T03:00:09 (הקודמת: 2026-10-04T03:00:12)" in out
    for line in ["חדשים: 5", "השתנו: 43", "הוסרו מהאתר: 2", "ללא שינוי: 7322", "שגיאות: 1"]:
        assert line in out
    assert "דפים ריקים שדולגו: 1" in out and "מאמרים באינדקס: 12" in out
    assert "מקטעים באינדקס: 98140 (+140)" in out
    assert "זמן: 25.5 דקות" in out and "טוקנים: 139,000" in out and "עלות: 0.0028$" in out
    listed = out.split("עודכנו לאחרונה:\n", 1)[1].splitlines()
    assert len(listed) == 10
    assert listed[0] == "  - דמי הבראה (12.10.2026)" and listed[-1] == "  - פנסיה (3.10.2026)"
    assert "עמוד שהוסר" not in out and "דף שנכשל" not in out and "דמי אבטלה" not in out
    posted = json.loads((crawl_root / "runs" / LATEST).read_text("utf-8"))["crawl"]
    assert "report_posted" not in posted

    sent = stub_telegram(monkeypatch)
    chat_ids(monkeypatch, "42")
    result = runner.invoke(app, ["crawl", "report", "--notify"])
    assert result.exit_code == 0, result.output
    [(path, body)] = sent
    assert path.endswith("/sendMessage") and body["chat_id"] == 42
    assert "חדשים: 5" in str(body["text"])
    records = sorted((crawl_root / "runs").glob("*.json"))
    flags = [json.loads(p.read_text("utf-8"))["crawl"].get("report_posted") for p in records]
    assert flags == [None, True]


def test_crawl_report_with_one_record_or_none(crawl_root: Path) -> None:
    (crawl_root / "runs" / "2026-10-04-030012-crawl.json").unlink()
    result = runner.invoke(app, ["crawl", "report"])
    assert result.exit_code == 0 and "(אין סריקה קודמת)" in result.output
    assert "(+0)" in result.output
    (crawl_root / "runs" / LATEST).unlink()
    result = runner.invoke(app, ["crawl", "report"])
    assert result.exit_code == 1 and "אין רשומת סריקה" in result.output


def test_criterion_5_notify_sends_one_message_to_the_allowed_chat(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """criterion 5: "`zchut notify "<text>"` sends one Telegram message to the allowed chat id with a stubbed transport and refuses when no allowed id is configured." """
    sent = stub_telegram(monkeypatch)
    chat_ids(monkeypatch, " 42 ")
    result = runner.invoke(app, ["notify", "דוח <b>חדש</b> & עוד"])
    assert result.exit_code == 0, result.output
    [(path, body)] = sent
    assert path == "/bottest-token/sendMessage"
    assert body["chat_id"] == 42 and body["parse_mode"] == "HTML"
    assert body["text"] == "דוח &lt;b&gt;חדש&lt;/b&gt; &amp; עוד"

    for ids in ["", "42,43"]:
        chat_ids(monkeypatch, ids)
        result = runner.invoke(app, ["notify", "שלום"])
        assert result.exit_code == 1 and "exactly one chat id" in result.output
    assert len(sent) == 1
