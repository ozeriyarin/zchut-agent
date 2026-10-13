from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from zchut.answer.model import AnswerError, AnswerModel, HaikuAnswerModel, make_haiku_client
from zchut.answer.prompt import Prompt
from zchut.config import ConfigError, Secrets, Settings
from zchut.http_retry import send_with_backoff
from zchut.types import ModelReply, Usage

TIERS = ("haiku", "qwen-openrouter", "vllm")
VERIFIERS = ("haiku", "jev")
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


def parse_choices(text: str, allowed: tuple[str, ...]) -> list[str]:
    chosen = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [c for c in chosen if c not in allowed]
    if unknown or not chosen:
        raise ConfigError(
            f"unknown choice {', '.join(unknown) or text!r}; use {', '.join(allowed)}"
        )
    return chosen


def chat_request(model: str, prompt: Prompt, max_tokens: int) -> dict[str, object]:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
    }


class OpenAICompatibleModel:
    def __init__(
        self,
        client: httpx.Client,
        url: str,
        model: str,
        api_key: str | None,
        max_tokens: int = 800,
        *,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.url = url
        self._client = client
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._max_attempts = max_attempts
        self._sleep = sleep

    def answer(self, prompt: Prompt) -> ModelReply:
        body = chat_request(self.model, prompt, self.max_tokens)
        response, attempts = send_with_backoff(
            lambda: self._post(body), self._max_attempts, self._sleep
        )
        if response.status_code != 200:
            raise AnswerError(
                f"{self.model} answered {response.status_code} after {attempts} attempt(s)"
            )
        try:
            payload = response.json()
            text = str(payload["choices"][0]["message"]["content"])
            u = payload["usage"]
            usage = Usage(int(u["prompt_tokens"]), int(u.get("completion_tokens", 0)))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AnswerError(f"unexpected {self.model} response: {exc}") from exc
        return ModelReply(text=text, usage=usage)

    def _post(self, body: dict[str, object]) -> httpx.Response:
        try:
            return self._client.post(self.url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise AnswerError(f"{self.model} request failed: {type(exc).__name__}") from exc


def make_answer_model(
    tier: str, secrets: Secrets, settings: Settings, client: httpx.Client | None = None
) -> AnswerModel:
    answer, models = settings.answer, settings.models
    if tier == "haiku":
        haiku = make_haiku_client(secrets.require_anthropic(), answer.timeout_s)
        return HaikuAnswerModel(haiku, models.answer, answer.max_tokens)
    key: str | None
    if tier == "qwen-openrouter":
        url, model, key = OPENROUTER_CHAT_URL, models.qwen, secrets.require_openrouter()
    elif tier == "vllm":
        url = f"{secrets.require_vllm_url().rstrip('/')}/chat/completions"
        model, key = models.vllm, secrets.vllm_api_key
    else:
        raise ConfigError(f"unknown tier {tier}; use {', '.join(TIERS)}")
    http = client or httpx.Client(timeout=answer.timeout_s)
    return OpenAICompatibleModel(http, url, model, key, answer.max_tokens)
