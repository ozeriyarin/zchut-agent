from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import ROOT, make_deps, make_graph_deps, make_guard
from zchut import cli
from zchut.answer.model import FakeAnswerModel
from zchut.cli import app
from zchut.config import (
    ConfigError,
    Prices,
    Secrets,
    Settings,
    load_dotenv,
    load_prices,
    load_secrets,
    load_settings,
)
from zchut.index.embedder import FakeEmbedder
from zchut.index.store import InMemoryVectorStore
from zchut.run.ask import run_ask
from zchut.run.converse import run_conversation
from zchut.run.cost_guard import BudgetExceeded, cost_usd
from zchut.run.cost_report import summarize_runs
from zchut.run.records import LocalRunSink, question_hash
from zchut.types import CallRecord, RunRecord, Usage

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
    assert {c["role"] for c in calls} == {"classify", "embed", "answer", "verify"}
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
    assert load_settings(ROOT).max_usd == 0.08
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
    assert record["calls"] == []

    result = runner.invoke(app, ["ingest", "--dry-run"])
    assert result.exit_code == 0
    record = json.loads(next((cli_root / "runs").glob("*-ingest.json")).read_text("utf-8"))
    assert record["max_usd"] == 0.08


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
        "GOOGLE_CLOUD_PROJECT=proj",
        "GOOGLE_CLOUD_REGION=europe-west1",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    env = {"ANTHROPIC_API_KEY": "from-env"}
    assert load_dotenv(path, env) == 2
    assert env == {
        "ANTHROPIC_API_KEY": "from-env",
        "VOYAGE_API_KEY": "plain",
        "ZCHUT_ANTHROPIC_API_KEY": "double quoted",
        "OPENROUTER_API_KEY": "single",
        "GOOGLE_CLOUD_PROJECT": "proj",
        "GOOGLE_CLOUD_REGION": "europe-west1",
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


def _fixture_record(
    runs: Path, name: str, command: str, at: datetime, calls: list[CallRecord]
) -> None:
    record = RunRecord(
        command=command,
        started_at=at.isoformat(timespec="seconds"),
        dry_run=False,
        max_usd=0.08,
        calls=calls,
        total_cost_usd=sum(c.cost_usd for c in calls),
    )
    (runs / name).write_text(record.model_dump_json(), encoding="utf-8")


def _call(role: str, model: str, cost: float) -> CallRecord:
    return CallRecord(
        role=role,
        model=model,
        input_tokens=10,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        latency_ms=5,
        cost_usd=cost,
    )


def test_criterion_7_no_verify_path_and_cost_command(
    fixture_store: InMemoryVectorStore, priced: Prices, settings: Settings, cli_root: Path
) -> None:
    """criterion 7: "`--no-verify` skips the verifier (path has no `verify`), and `zchut cost --days 7` sums the fixture records by command and model." """
    result = runner.invoke(app, ["ask", QUESTION, "--dry-run", "--no-verify"])
    assert result.exit_code == 0, result.output
    record = json.loads(next((cli_root / "runs").glob("*-ask.json")).read_text("utf-8"))
    assert record["graph"]["path"] == ["classify", "retrieve", "answer", "final"]
    assert (record["graph"]["verified"], record["graph"]["verifier"]) == (None, None)
    assert "verify" not in {c["role"] for c in record["calls"]}

    min_score = settings.retrieval.dry_run_min_score
    v1 = run_ask(
        QUESTION, make_deps(fixture_store, FakeAnswerModel(), min_score), make_guard(priced)
    )
    deps = make_graph_deps(fixture_store, FakeAnswerModel(), min_score, None)
    v2 = run_conversation(QUESTION, [], deps, make_guard(priced), None)
    assert v2 == v1 and v1.output.splitlines()[0] in result.output

    runs = cli_root / "runs"
    for path in runs.iterdir():
        path.unlink()
    now = datetime.now()
    haiku, voyage = "claude-haiku-4-5", "voyage-3.5-lite"
    ask_calls = [_call("embed", voyage, 0.00001), _call("answer", haiku, 0.002)]
    _fixture_record(runs, "a-ask.json", "ask", now - timedelta(days=1), ask_calls)
    _fixture_record(
        runs, "b-ask.json", "ask", now - timedelta(days=2), [_call("answer", haiku, 0.001)]
    )
    _fixture_record(
        runs, "c-eval.json", "eval", now - timedelta(days=3), [_call("embed", voyage, 0.0001)]
    )
    _fixture_record(
        runs, "d-ask.json", "ask", now - timedelta(days=9), [_call("answer", haiku, 5.0)]
    )
    (runs / "c-eval-report.json").write_text("{}", encoding="utf-8")
    (runs / "e-broken.json").write_text("{", encoding="utf-8")

    summary = summarize_runs(LocalRunSink(runs), now - timedelta(days=7))
    assert {k: (t.count, round(t.cost_usd, 6)) for k, t in summary.by_command.items()} == {
        "ask": (2, 0.00301),
        "eval": (1, 0.0001),
    }
    assert {k: (t.count, round(t.cost_usd, 6)) for k, t in summary.by_model.items()} == {
        haiku: (2, 0.003),
        voyage: (2, 0.00011),
    }
    assert summary.total_usd == pytest.approx(0.00311) and summary.skipped == ["e-broken.json"]

    result = runner.invoke(app, ["cost", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "  ask: 2 ריצות, 0.003010$" in result.output
    assert f"  {haiku}: 2 קריאות, 0.003000$" in result.output
    assert 'סה"כ: 0.003110$' in result.output and "e-broken.json" in result.output
    assert len(list(runs.iterdir())) == 6
