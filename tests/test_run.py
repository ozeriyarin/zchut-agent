from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import ROOT, make_deps, make_guard
from zchut import cli
from zchut.answer.model import FakeAnswerModel
from zchut.cli import app
from zchut.config import (
    ConfigError,
    Prices,
    Secrets,
    load_dotenv,
    load_prices,
    load_secrets,
    load_settings,
)
from zchut.index.embedder import FakeEmbedder
from zchut.index.store import InMemoryVectorStore
from zchut.run.ask import run_ask
from zchut.run.cost_guard import BudgetExceeded, cost_usd
from zchut.run.records import question_hash
from zchut.types import Usage

runner = CliRunner()
QUESTION = "מה צריך להביא לרישום בלשכת התעסוקה?"
KEY = "sk-test-do-not-leak-123"


def test_criterion_8_call_records_and_redaction(
    fixture_store: InMemoryVectorStore, priced: Prices, tmp_path: Path
) -> None:
    """criterion 8: "Every embed and answer call is recorded with role, model, input and output tokens, cache read tokens, latency and a cost computed from `config/prices.json`; the record total equals the sum of per-call costs and never contains an API key or the question text (redaction test)." """
    guard = make_guard(priced)
    run_ask(QUESTION, make_deps(fixture_store, FakeAnswerModel(), 0.3), guard)
    record = guard.recorder.record
    assert [c.role for c in record.calls] == ["embed", "answer"]
    for call in record.calls:
        usage = Usage(
            call.input_tokens, call.output_tokens, call.cache_read_tokens, call.cache_write_tokens
        )
        assert call.cost_usd == cost_usd(priced, call.model, usage) > 0
        assert call.latency_ms >= 0 and call.input_tokens > 0
    assert record.calls[1].output_tokens > 0
    assert record.total_cost_usd == pytest.approx(sum(c.cost_usd for c in record.calls))

    haiku = load_prices(ROOT).for_model("claude-haiku-4-5")
    assert (haiku.input, haiku.output, haiku.cache_read, haiku.cache_write) == (1, 5, 0.1, 1.25)
    assert cost_usd(load_prices(ROOT), "claude-haiku-4-5", Usage(1000, 100, 2000)) == (
        pytest.approx((1000 * 1 + 100 * 5 + 2000 * 0.1) / 1_000_000)
    )

    record.errors.append(f"upstream said: bad key {KEY} for {QUESTION}")
    text = guard.recorder.write(tmp_path / "runs", [KEY, QUESTION]).read_text("utf-8")
    assert KEY not in text and QUESTION not in text
    assert json.loads(text)["question_hash"] == question_hash(QUESTION)


def test_criterion_8_cli_record_has_no_key_or_question(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """criterion 8: "Every embed and answer call is recorded with role, model, input and output tokens, cache read tokens, latency and a cost computed from `config/prices.json`; the record total equals the sum of per-call costs and never contains an API key or the question text (redaction test)." """
    monkeypatch.setenv("VOYAGE_API_KEY", KEY)
    result = runner.invoke(app, ["ask", QUESTION, "--dry-run"])
    assert result.exit_code == 0, result.output
    text = next((cli_root / "runs").glob("*-ask.json")).read_text("utf-8")
    assert KEY not in text and QUESTION not in text
    calls = json.loads(text)["calls"]
    assert {c["role"] for c in calls} == {"embed", "answer"}
    assert set(calls[0]) >= {
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "latency_ms",
        "cost_usd",
    }


def test_criterion_9_cost_guard_aborts_before_the_call(
    fixture_store: InMemoryVectorStore,
    priced: Prices,
    cli_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """criterion 9: "Cost guard: with `--max-usd` set below the estimate of the next call, the command aborts before that call with a Hebrew message and exit code 2, the record shows `budget_exceeded: true`, and the default cap is 0.05." """
    assert load_settings(ROOT).max_usd == 0.05
    model = FakeAnswerModel()
    guard = make_guard(priced, max_usd=0.001)
    outcome = run_ask(QUESTION, make_deps(fixture_store, model, 0.3), guard)
    assert outcome.exit_code == 2 and "התקציב" in outcome.output
    assert model.calls == 0
    assert guard.recorder.record.budget_exceeded is True
    assert [c.role for c in guard.recorder.record.calls] == ["embed"]
    with pytest.raises(BudgetExceeded):
        make_guard(priced, max_usd=0.0).embed(FakeEmbedder(), ["א"], "query")

    monkeypatch.setattr(Prices, "with_free", lambda self, models: priced)
    result = runner.invoke(app, ["ask", QUESTION, "--dry-run", "--max-usd", "0.001"])
    assert result.exit_code == 2
    assert "הריצה נעצרה לפני קריאה שהייתה חוצה את תקרת התקציב" in result.output
    record = json.loads(next((cli_root / "runs").glob("*-ask.json")).read_text("utf-8"))
    assert record["budget_exceeded"] is True and record["max_usd"] == 0.001
    assert [c["role"] for c in record["calls"]] == ["embed"]

    result = runner.invoke(app, ["ingest", "--dry-run"])
    assert result.exit_code == 0
    record = json.loads(next((cli_root / "runs").glob("*-ingest.json")).read_text("utf-8"))
    assert record["max_usd"] == 0.05


def test_secrets_from_env_and_fail_fast() -> None:
    secrets = Secrets.from_env({"ANTHROPIC_API_KEY": "a", "VOYAGE_API_KEY": "v"})
    assert secrets.require_anthropic() == "a" and secrets.require_voyage() == "v"
    assert (
        Secrets.from_env(
            {"ZCHUT_ANTHROPIC_API_KEY": "z", "ANTHROPIC_API_KEY": "a"}
        ).require_anthropic()
        == "z"
    )
    with pytest.raises(ConfigError, match="VOYAGE_API_KEY"):
        Secrets.from_env({}).require_voyage()
    with pytest.raises(ConfigError, match="ZCHUT_ANTHROPIC_API_KEY"):
        Secrets.from_env({"ANTHROPIC_API_KEY": ""}).require_anthropic()


def test_ask_without_keys_fails_fast_with_variable_name(cli_root: Path) -> None:
    result = runner.invoke(app, ["ask", QUESTION])
    assert result.exit_code == 1
    assert "VOYAGE_API_KEY" in result.output
    assert not (cli_root / "runs").exists()


def test_load_dotenv_parses_pairs_and_keeps_existing_values(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    lines = [
        "# comment",
        "",
        "VOYAGE_API_KEY=plain",
        'ZCHUT_ANTHROPIC_API_KEY="double quoted"',
        "OPENROUTER_API_KEY='single'",
        "  ANTHROPIC_API_KEY = value with = sign  ",
        "NO_EQUALS_LINE",
        "HTTPS_PROXY=http://evil.example",
        "SSL_CERT_FILE=/tmp/evil.pem",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    env = {"ANTHROPIC_API_KEY": "from-env"}
    assert load_dotenv(path, env) == 2
    assert env == {
        "ANTHROPIC_API_KEY": "from-env",
        "VOYAGE_API_KEY": "plain",
        "ZCHUT_ANTHROPIC_API_KEY": "double quoted",
        "OPENROUTER_API_KEY": "single",
    }
    spaced: dict[str, str] = {}
    load_dotenv(path, spaced)
    assert spaced["ANTHROPIC_API_KEY"] == "value with = sign"
    missing: dict[str, str] = {}
    assert load_dotenv(tmp_path / "absent.env", missing) == 0
    assert missing == {}


def test_load_secrets_reads_the_dotenv_file(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "registered-for-teardown")
    monkeypatch.delenv("VOYAGE_API_KEY")
    (cli_root / ".env").write_text(f"VOYAGE_API_KEY={KEY}\n", encoding="utf-8")
    assert load_secrets(cli._dotenv_path()).voyage_api_key == KEY
    assert os.environ["VOYAGE_API_KEY"] == KEY


def test_dotenv_path_is_the_package_root_never_cwd(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = cli_root / "package"
    monkeypatch.setattr(cli, "PACKAGE_ROOT", package)
    (cli_root / ".env").write_text("", encoding="utf-8")
    assert cli._dotenv_path() == package / ".env"


def test_unknown_model_price_is_an_error() -> None:
    with pytest.raises(ConfigError, match="no price"):
        load_prices(ROOT).for_model("claude-unknown")
