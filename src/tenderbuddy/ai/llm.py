"""LLM clients: any OpenAI-compatible chat API, plus a deterministic offline mock.

OpenAI-compatible providers that work out of the box (set ``LLM_BASE_URL``):

* Groq          https://api.groq.com/openai/v1   (free tier, needs LLM_API_KEY)
* OpenRouter    https://openrouter.ai/api/v1     (free models, needs LLM_API_KEY)
* Ollama        http://localhost:11434/v1        (local, no key)
* OpenAI        https://api.openai.com/v1
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from tenderbuddy.config import Settings

Message = dict[str, str]
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it of on or that the this to was were will with "
    "shall must should may been being which who whom whose their its into per all any each".split()
)


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    name: str

    def complete(self, messages: list[Message], *, json_mode: bool = False, max_tokens: int = 1200) -> str: ...


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.split(" ".join(text.split())) if s.strip()]


def extractive_summary(text: str, max_sentences: int = 3) -> str:
    """Deterministic frequency-based extractive summary (keeps original sentence order)."""
    sents = sentences(text)
    if len(sents) <= max_sentences:
        return " ".join(sents)
    freq: dict[str, int] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok not in STOPWORDS:
            freq[tok] = freq.get(tok, 0) + 1

    def rank(i: int) -> tuple[float, int]:
        toks = {t for t in _TOKEN_RE.findall(sents[i].lower()) if t not in STOPWORDS}
        return (-sum(freq[t] for t in toks) / (1 + len(toks)) ** 0.5, i)

    keep = sorted(sorted(range(len(sents)), key=rank)[:max_sentences])
    return " ".join(sents[i] for i in keep)


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in a model reply (tolerates ``` fences and prose)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("model reply was not valid JSON")


class MockLLM:
    """Offline deterministic 'model'. AI features detect it and use their rule-based path."""

    name = "mock"

    def complete(self, messages: list[Message], *, json_mode: bool = False, max_tokens: int = 1200) -> str:
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        summary = extractive_summary(user, 3)
        return json.dumps({"summary": summary}) if json_mode else summary


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: float = 60.0, max_retries: int = 2,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if not base_url:
            raise LLMError("LLM_BASE_URL is required for the openai provider")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.name = f"openai-compatible:{model}"
        self._client = client or httpx.Client(timeout=timeout)
        self._sleep = sleep

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def complete(self, messages: list[Message], *, json_mode: bool = False, max_tokens: int = 1200) -> str:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.2,
                                "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        attempt = 0
        while True:
            status: int | None = None
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", headers=self.headers(), json=body)
                status = resp.status_code
                if status == 400 and "response_format" in body:
                    body.pop("response_format")  # some compatible servers reject it; retry without
                    continue
                if status in RETRY_STATUS and attempt < self.max_retries:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retryable = isinstance(exc, httpx.TransportError) or status in RETRY_STATUS
                if not retryable or attempt >= self.max_retries:
                    raise LLMError(f"LLM request failed ({f'HTTP {status}' if status else type(exc).__name__})") \
                        from exc
                self._sleep(min(20.0, 2.0**attempt))
                attempt += 1
                continue
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise LLMError("unexpected LLM response shape") from exc
            if not isinstance(content, str) or not content.strip():
                raise LLMError("empty completion")
            return content.strip()


def get_llm(settings: Settings, client: httpx.Client | None = None) -> LLMClient:
    if settings.llm_mode == "openai":
        return OpenAICompatibleLLM(settings.LLM_BASE_URL, settings.LLM_API_KEY, settings.LLM_MODEL,
                                   timeout=settings.LLM_TIMEOUT_SECONDS, client=client)
    return MockLLM()
