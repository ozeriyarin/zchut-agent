from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from conftest import CLARIFICATIONS, make_graph_deps, make_guard
from zchut.answer.model import AnswerError
from zchut.answer.prompt import build_prompt
from zchut.answer.tiers import (
    OPENROUTER_CHAT_URL,
    TIERS,
    OpenAICompatibleModel,
    make_answer_model,
    parse_choices,
)
from zchut.config import ConfigError, Prices, Secrets, Settings
from zchut.graph.build import converse
from zchut.graph.clarifications import load_clarifications
from zchut.graph.classify import HaikuClassifier, classify_schema
from zchut.graph.verify import (
    OPENROUTER_DECISIONS_URL,
    VERIFY_SCHEMA,
    FakeVerifier,
    HaikuVerifier,
    JevVerifier,
    jev_request,
)
from zchut.index.store import InMemoryVectorStore
from zchut.run.cost_guard import cost_usd
from zchut.types import Usage

QUESTION = "מי זכאי לדמי אבטלה?"
SECRETS = Secrets(
    openrouter_api_key="or-test-key", vllm_url="http://gpu.test:8000/v1/", vllm_api_key="vk"
)


def completion(text: str) -> httpx.Response:
    choice = {"message": {"role": "assistant", "content": text}}
    usage = {"prompt_tokens": 120, "completion_tokens": 30}
    return httpx.Response(200, json={"choices": [choice], "usage": usage})


def anthropic_client(
    payloads: list[dict[str, object]], sent: list[dict[str, object]]
) -> anthropic.Anthropic:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        text = json.dumps(payloads[len(sent) - 1], ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 50, "output_tokens": 10},
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return anthropic.Anthropic(api_key="test-key", http_client=http)


def test_criterion_4_tiers_and_verifiers_over_stubbed_transports(
    fixture_store: InMemoryVectorStore, priced: Prices, settings: Settings
) -> None:
    """criterion 4: "`--tier qwen-openrouter` sends the same prompt to the OpenAI-compatible endpoint with the configured model id (stubbed transport), prices the reply under that id, and the verifier still runs on its own tier; `--tier vllm` targets `ZCHUT_VLLM_URL`; `--verifier jev` sends one decisions request with the choices `supported` / `unsupported` and calls Haiku for the claims only on `unsupported`." """
    sent: list[httpx.Request] = []

    def chat(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return completion("זכאים מי שנרשמו בשירות התעסוקה [1].")

    http = httpx.Client(transport=httpx.MockTransport(chat))
    qwen = make_answer_model("qwen-openrouter", SECRETS, settings, http)
    guard = make_guard(priced)
    deps = make_graph_deps(fixture_store, qwen, 0.3, FakeVerifier(), "qwen-openrouter")
    state = converse(deps, guard, QUESTION, [], None)
    prompt = build_prompt(QUESTION, state.hits)
    [request] = sent
    assert str(request.url) == OPENROUTER_CHAT_URL
    assert request.headers["Authorization"] == "Bearer or-test-key"
    assert json.loads(request.content) == {
        "model": "qwen/qwen3-32b",
        "max_tokens": settings.answer.max_tokens,
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
    }
    calls = {c.role: c for c in guard.recorder.record.calls}
    assert calls["answer"].model == "qwen/qwen3-32b"
    assert calls["answer"].cost_usd == cost_usd(priced, "qwen/qwen3-32b", Usage(120, 30)) > 0
    assert calls["verify"].model == "fake-verifier"
    assert state.path[-2:] == ["verify", "final"]

    vllm = make_answer_model("vllm", SECRETS, settings, http)
    vllm.answer(prompt)
    assert str(sent[-1].url) == "http://gpu.test:8000/v1/chat/completions"
    assert json.loads(sent[-1].content)["model"] == "vllm"
    assert sent[-1].headers["Authorization"] == "Bearer vk"

    decisions: list[dict[str, object]] = []
    choice = "supported"

    def decide(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OPENROUTER_DECISIONS_URL
        decisions.append(json.loads(request.content))
        answers = {"decision": {"type": "choice", "choice": choice, "confidence": 0.9}}
        usage = {"input_tokens": 400, "output_tokens": 0, "cost": 0.00004}
        return httpx.Response(200, json={"answers": answers, "usage": usage})

    haiku_sent: list[dict[str, object]] = []
    claims = {"supported": False, "unsupported_claims": ["דמי אבטלה לכל החיים"]}
    lister = HaikuVerifier(anthropic_client([claims], haiku_sent), "claude-haiku-4-5", 400)
    jev_http = httpx.Client(transport=httpx.MockTransport(decide))
    jev = JevVerifier(jev_http, "or-test-key", "typesafe/jev-1.13", settings.graph.jev, lister)
    draft = state.draft
    assert draft is not None
    jev_guard = make_guard(priced)
    assert jev.verify(draft.text, state.hits, jev_guard).supported is True
    assert len(decisions) == 1 and haiku_sent == []
    assert decisions[0] == jev_request(
        "typesafe/jev-1.13", draft.text, state.hits, settings.graph.jev
    )
    question = decisions[0]["questions"]["decision"]  # type: ignore[index]
    assert question["type"] == "choice" and set(question["criteria"]) == {
        "supported",
        "unsupported",
    }

    choice = "unsupported"
    verdict = jev.verify(draft.text, state.hits, jev_guard)
    assert (verdict.supported, verdict.unsupported_claims) == (False, ["דמי אבטלה לכל החיים"])
    assert len(decisions) == 2 and len(haiku_sent) == 1
    assert [(c.role, c.model) for c in jev_guard.recorder.record.calls] == [
        ("verify", "typesafe/jev-1.13"),
        ("verify", "typesafe/jev-1.13"),
        ("verify", "claude-haiku-4-5"),
    ]
    assert jev_guard.recorder.record.calls[0].cost_usd == pytest.approx(400 * 0.1 / 1_000_000)


def test_haiku_classifier_and_verifier_send_json_schemas(priced: Prices) -> None:
    sent: list[dict[str, object]] = []
    payload = {"topic": "tax", "in_scope": True, "missing_fact": None}
    client = anthropic_client([payload, {"supported": True, "unsupported_claims": []}], sent)
    guard = make_guard(priced)
    facts = load_clarifications(CLARIFICATIONS)
    result = HaikuClassifier(client, "claude-haiku-4-5", 200, facts).classify("החזר מס?", guard)
    assert (result.topic, result.in_scope, result.missing_fact) == ("tax", True, None)
    verdict = HaikuVerifier(client, "claude-haiku-4-5", 400).verify("א [1].", [], guard)
    assert verdict.supported is True
    assert [b["output_config"] for b in sent] == [
        {"format": {"type": "json_schema", "schema": classify_schema(facts)}},
        {"format": {"type": "json_schema", "schema": VERIFY_SCHEMA}},
    ]
    assert [c.role for c in guard.recorder.record.calls] == ["classify", "verify"]


def test_openai_compatible_model_backs_off_then_fails_cleanly() -> None:
    statuses = [429, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 200:
            return completion("תשובה")
        return httpx.Response(status, headers={"Retry-After": "3"})

    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler))
    model = OpenAICompatibleModel(
        http, "http://x.test/v1/chat/completions", "m", None, sleep=sleeps.append
    )
    reply = model.answer(build_prompt("שאלה?", []))
    assert reply.text == "תשובה" and sleeps == [3.0]

    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    failing = OpenAICompatibleModel(
        down, "http://x.test", "m", None, max_attempts=2, sleep=sleeps.append
    )
    with pytest.raises(AnswerError, match="answered 503 after 2 attempt"):
        failing.answer(build_prompt("שאלה?", []))


def test_tier_selection_needs_its_keys(settings: Settings) -> None:
    assert parse_choices("haiku, qwen-openrouter", TIERS) == ["haiku", "qwen-openrouter"]
    with pytest.raises(ConfigError, match="unknown choice"):
        parse_choices("haiku,gpt", TIERS)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        make_answer_model("qwen-openrouter", Secrets(), settings)
    with pytest.raises(ConfigError, match="ZCHUT_VLLM_URL"):
        make_answer_model("vllm", Secrets(), settings)
    secrets = Secrets.from_env({"ZCHUT_VLLM_URL": "http://h/v1", "ZCHUT_VLLM_API_KEY": "k"})
    assert (secrets.vllm_url, secrets.vllm_api_key) == ("http://h/v1", "k")
    assert "k" in secrets.values()
