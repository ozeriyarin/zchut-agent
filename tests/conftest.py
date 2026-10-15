from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

from zchut import cli
from zchut.answer.model import FAKE_ANSWER_MODEL, AnswerModel, FakeAnswerModel
from zchut.config import ModelPrice, Prices, Settings, load_prices, load_settings
from zchut.graph.build import GraphDeps
from zchut.graph.clarifications import load_clarifications
from zchut.graph.classify import FAKE_CLASSIFIER_MODEL, FakeClassifier
from zchut.graph.condense import FAKE_CONDENSER_MODEL, FakeCondenser
from zchut.graph.offices import load_offices
from zchut.graph.verify import FAKE_VERIFIER_MODEL, Verifier
from zchut.index.embedder import FAKE_EMBED_MODEL, FakeEmbedder
from zchut.index.ingest import ingest
from zchut.index.store import InMemoryVectorStore
from zchut.run.ask import AskDeps
from zchut.run.cost_guard import CostGuard
from zchut.run.records import RunRecorder
from zchut.sources.fetch import FixtureFetcher

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "pages"
AVTALA = "https://www.kolzchut.org.il/he/דמי_אבטלה"
PENSIA = "https://www.kolzchut.org.il/he/פנסיה_וחיסכון_ארוך_טווח_לשכירים"
TAX = "https://www.gov.il/he/service/income-tax-refund-for-employees"
OFFICES = ROOT / "config" / "offices.json"
CLARIFICATIONS = ROOT / "config" / "clarifications.json"
EMPLOYMENT_QUESTION = load_clarifications(CLARIFICATIONS)["employment_status"].question_he


@pytest.fixture
def settings() -> Settings:
    return load_settings(ROOT)


@pytest.fixture
def priced() -> Prices:
    fakes = {
        FAKE_EMBED_MODEL: ModelPrice(input=0.02),
        FAKE_ANSWER_MODEL: ModelPrice(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25),
        FAKE_CLASSIFIER_MODEL: ModelPrice(input=1.0, output=5.0),
        FAKE_CONDENSER_MODEL: ModelPrice(input=1.0, output=5.0),
        FAKE_VERIFIER_MODEL: ModelPrice(input=1.0, output=5.0),
    }
    return Prices(models={**load_prices(ROOT).models, **fakes})


def make_guard(prices: Prices, max_usd: float = math.inf) -> CostGuard:
    return CostGuard(max_usd, prices, RunRecorder("test", True, max_usd))


@pytest.fixture
def fixture_store(settings: Settings, priced: Prices) -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    fetcher = FixtureFetcher(FIXTURES)
    chunk = settings.chunk
    ingest(
        fetcher.urls(),
        fetcher,
        FakeEmbedder(),
        store,
        make_guard(priced),
        chunk.max_chars,
        chunk.min_chars,
    )
    return store


def make_deps(store: InMemoryVectorStore, model: FakeAnswerModel, min_score: float) -> AskDeps:
    return AskDeps(FakeEmbedder(), store, model, 5, min_score)


def make_graph_deps(
    store: InMemoryVectorStore,
    model: AnswerModel,
    min_score: float,
    verifier: Verifier | None,
    tier: str = "haiku",
) -> GraphDeps:
    ask = AskDeps(FakeEmbedder(), store, model, 5, min_score)
    return GraphDeps(
        ask,
        FakeClassifier(),
        FakeCondenser(),
        verifier,
        load_offices(OFFICES),
        load_clarifications(CLARIFICATIONS),
        5 * 700,
        tier,
    )


@pytest.fixture
def cli_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(FIXTURES, tmp_path / "tests" / "fixtures" / "pages")
    shutil.copytree(ROOT / "eval", tmp_path / "eval")
    cfg_path = tmp_path / "config" / "zchut.json"
    cfg = json.loads(cfg_path.read_text("utf-8"))
    cfg["store"]["backend"] = "chroma"  # tests use the local store whatever the repo config says
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "PACKAGE_ROOT", tmp_path)
    for name in ("VOYAGE_API_KEY", "ZCHUT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path
