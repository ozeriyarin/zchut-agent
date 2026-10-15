from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from typer.testing import CliRunner

from conftest import FIXTURES, ROOT, make_guard
from zchut.answer.model import FakeAnswerModel
from zchut.cli import app
from zchut.config import Prices, Settings
from zchut.eval.questions import load_questions
from zchut.index.embedder import FakeEmbedder
from zchut.index.ingest import ingest
from zchut.index.keyword import KeywordIndex, fuse, load_keyword_index, tokenize
from zchut.index.manifest import Manifest, ManifestEntry
from zchut.index.store import ChromaStore, InMemoryVectorStore, QdrantStore, VectorStore
from zchut.run.ask import AskDeps, retrieve, search
from zchut.sources.fetch import FixtureFetcher
from zchut.types import Chunk, Hit

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect")
runner = CliRunner()


def chunk(chunk_id: str, section: str, text: str) -> Chunk:
    url = f"https://www.kolzchut.org.il/he/{chunk_id}"
    return Chunk(chunk_id, url, section, section, text, "2026-10-15", "CC-BY-SA-3.0", None)


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("והפנסיה", ["פנסיה"]),
        ("בשירות שירות", ["ירות", "ירות"]),
        ("לבית הבית בית", ["בית", "בית", "בית"]),
        ("מס", ["מס"]),
        ("גיל 67, שנת 2026", ["גיל", "67", "שנת", "2026"]),
        ("ב-2026", ["ב", "2026"]),
        ("Bituach Leumi", ["bituach", "leumi"]),
    ],
)
def test_tokenizer_strips_prefixes_and_keeps_digits(text: str, tokens: list[str]) -> None:
    assert tokenize(text) == tokens


def test_criterion_1_tokenizer_and_bm25_title_path_ranking() -> None:
    """criterion 1: "The tokenizer strips Hebrew prefixes and keeps digits (table-driven), and the BM25 index ranks a chunk whose title path contains the question's exact words above a chunk that only paraphrases them (fixture)." """
    assert tokenize("ובפנסיה לגיל 67") == tokenize("פנסיה גיל 67")
    titled = chunk("titled", "פנסיה > חישוב הקצבה", "הסכום נקבע לפי השכר הקובע והוותק.")
    paraphrase = chunk("paraphrase", "זכויות > ותיקים", "איך מחשבים את התשלום החודשי לפורשים?")
    filler = [chunk(f"f{n}", "אבטלה > הרשמה", f"נרשמים בלשכה מספר {n}.") for n in range(3)]
    index = KeywordIndex([paraphrase, *filler, titled])
    assert [c.id for c in index.search("איך מתבצע חישוב הקצבה בפנסיה?", 5)] == [
        "titled",
        "paraphrase",
    ]
    assert index.search("", 5) == [] and KeywordIndex([]).search("פנסיה", 5) == []


def _hit(chunk_id: str, score: float) -> Hit:
    return Hit(chunk=chunk(chunk_id, "כותרת", "טקסט"), score=score)


def test_criterion_2_fusion_keeps_vector_scores_and_vector_mode_is_v1(
    fixture_store: InMemoryVectorStore,
) -> None:
    """criterion 2: "Reciprocal rank fusion merges two rankings deterministically, keeps vector scores on the fused hits, and `retrieval.mode: vector` reproduces the v1 order exactly." """
    vector = [_hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)]
    keyword = [chunk("c", "כותרת", "טקסט"), chunk("d", "כותרת", "טקסט"), vector[1].chunk]
    fused = fuse(vector, keyword, 4)
    assert [(h.chunk.id, h.score) for h in fused] == [
        ("c", 0.7),
        ("b", 0.8),
        ("a", 0.9),
        ("d", 0.7),
    ]
    assert fuse(vector, keyword, 4) == fused
    assert [h.chunk.id for h in fuse(vector, keyword, 2)] == ["c", "a"]
    assert fuse(vector, [], 2) == vector[:2] and fuse([], [], 2) == []

    keyword_index = KeywordIndex(fixture_store.chunks())
    embedder = FakeEmbedder()
    vector_deps = AskDeps(embedder, fixture_store, FakeAnswerModel(), 5, 0.3)
    hybrid_deps = AskDeps(embedder, fixture_store, FakeAnswerModel(), 5, 0.3, keyword_index)
    reordered = 0
    for q in load_questions(ROOT / "eval" / "questions.json"):
        [query] = embedder.embed([q.question], "query").vectors
        v1 = fixture_store.search(query, 5)
        assert search(q.question, query, vector_deps) == v1
        hybrid = search(q.question, query, hybrid_deps)
        assert max(h.score for h in hybrid) == v1[0].score
        reordered += hybrid != v1
    assert reordered > 0


class CountingStore(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.scans = 0

    def chunks(self) -> Iterator[Chunk]:
        self.scans += 1
        return super().chunks()


def _manifest(fetched_at: str) -> Manifest:
    entry = ManifestEntry(revid=1, last_updated=None, fetched_at=fetched_at, chunks=1, status="ok")
    return Manifest(
        articles={"a": entry, "b": entry.model_copy(update={"fetched_at": "2026-01-01"})}
    )


def test_criterion_3_keyword_index_cache_rebuilds_only_on_key_change(tmp_path: Path) -> None:
    """criterion 3: "The keyword index is cached on disk and rebuilt only when the store count or the manifest's newest fetch date changes (fake store, temp dir)." """
    store, path = CountingStore(), tmp_path / "data" / "keyword-index.pkl"
    first = chunk("first", "פנסיה > חישוב", "קצבת זיקנה")
    filler = [chunk(f"f{n}", "אבטלה > הרשמה", "נרשמים בלשכה") for n in range(3)]
    store.replace(first.url, [first, *filler], [[1.0, 0.0]] * 4)
    manifest = _manifest("2026-10-14")

    assert [c.id for c in load_keyword_index(store, manifest, path).search("פנסיה", 5)] == ["first"]
    assert path.exists() and store.scans == 1
    assert [c.id for c in load_keyword_index(store, manifest, path).search("פנסיה", 5)] == ["first"]
    assert store.scans == 1

    second = chunk("second", "פנסיה > הפקדות", "הפקדות לפנסיה")
    store.replace(second.url, [second], [[0.0, 1.0]])
    assert len(load_keyword_index(store, manifest, path).search("פנסיה", 5)) == 2
    assert store.scans == 2
    load_keyword_index(store, _manifest("2026-10-15"), path)
    assert store.scans == 3
    load_keyword_index(store, _manifest("2026-10-15"), path)
    assert store.scans == 3

    path.write_bytes(b"not a pickle")
    assert len(load_keyword_index(store, _manifest("2026-10-15"), path).search("פנסיה", 5)) == 2
    assert store.scans == 4


def test_every_store_lists_all_its_chunks(
    settings: Settings, priced: Prices, tmp_path: Path
) -> None:
    fetcher = FixtureFetcher(FIXTURES)
    stores: list[VectorStore] = [
        InMemoryVectorStore(),
        ChromaStore(tmp_path / "chroma"),
        QdrantStore(QdrantClient(":memory:"), 512),
    ]
    for store in stores:
        ingest(
            fetcher.urls(),
            fetcher,
            FakeEmbedder(512),
            store,
            make_guard(priced),
            settings.chunk.max_chars,
            settings.chunk.min_chars,
        )
    memory, chroma, qdrant = stores
    want = sorted(memory.chunks(), key=lambda c: c.id)
    assert len(want) == memory.count() > 7
    assert sorted(chroma.chunks(), key=lambda c: c.id) == want
    assert isinstance(qdrant, QdrantStore)
    assert sorted(qdrant.chunks(batch=7), key=lambda c: c.id) == want


def test_retrieve_uses_the_keyword_index_in_hybrid_mode(
    fixture_store: InMemoryVectorStore, priced: Prices
) -> None:
    index = KeywordIndex(fixture_store.chunks())
    deps = AskDeps(FakeEmbedder(), fixture_store, FakeAnswerModel(), 5, 0.3, index)
    guard = make_guard(priced)
    question = "מי זכאי לדמי אבטלה?"
    [query] = FakeEmbedder().embed([question], "query").vectors
    assert retrieve(question, deps, guard) == search(question, query, deps)
    assert [c.role for c in guard.recorder.record.calls] == ["embed"]


def _eval(cli_root: Path, *args: str) -> dict[str, object]:
    before = set((cli_root / "runs").glob("*-report.json"))
    result = runner.invoke(app, ["eval", *args, "--dry-run"])
    assert result.exit_code == 0, result.output
    [path] = set((cli_root / "runs").glob("*-report.json")) - before
    report: dict[str, object] = json.loads(path.read_text("utf-8"))
    return report


def test_criterion_4_hybrid_eval_does_not_regress(cli_root: Path) -> None:
    """criterion 4: "`zchut eval --retrieval-only --mode hybrid` runs on the fixture index and the dry-run metrics do not regress versus `--mode vector` on the fixture questions." """
    (cli_root / "runs").mkdir()
    vector = _eval(cli_root, "--retrieval-only", "--mode", "vector")
    hybrid = _eval(cli_root, "--retrieval-only", "--mode", "hybrid")
    assert isinstance(vector["hit_at_3"], float) and isinstance(hybrid["hit_at_3"], float)
    assert hybrid["hit_at_3"] >= vector["hit_at_3"]
    assert hybrid["refusal_rate"] == vector["refusal_rate"]
    assert hybrid["rows"] != vector["rows"]

    talks = [_eval(cli_root, "--conversations", "--mode", m) for m in ("vector", "hybrid")]
    assert talks[1]["turn_accuracy"] == talks[0]["turn_accuracy"] == 1.0
    assert not (cli_root / "data").exists()
