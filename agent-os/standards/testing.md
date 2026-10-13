# Testing

- pytest. Tests live under `tests/` as `test_<module>.py`; shared fixtures in `tests/conftest.py`, saved pages under `tests/fixtures/`.
- Offline only: no network in tests, ever. The fetcher, the embedder, the vector store and the answer model are tested through fakes; the fakes are deterministic (hash-based vectors, canned answers) so metrics are reproducible.
- Pure transforms get table-driven tests (`pytest.mark.parametrize`): HTML -> sections, chunking with title paths, citation numbering, refusal threshold, metric math.
- Fixtures are real captured pages, trimmed, with their fetch date and license noted in a sibling `.meta.json`; never edit fixture text by hand to make a test pass.
- `run/` is tested end to end with fakes: an ingest of the fixtures produces N chunks with full metadata; an ask produces one answer, citations and one record; a weak retrieval produces the refusal and no model call.
- Every spec's acceptance criteria map to at least one test whose name quotes the criterion: the test function is `test_criterion_<n>_<slug>` and its docstring's first line is `criterion <n>: "<criterion text>"`; `tests/test_spec_coverage.py` enforces it.
- `pytest`, `ruff check` and `mypy` must pass before a commit.
