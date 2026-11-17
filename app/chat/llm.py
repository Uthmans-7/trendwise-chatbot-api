"""Minimal async client for OpenAI-compatible chat APIs (Groq, OpenRouter).

Rate limits: a per-model throttle spaces request starts (RPM) and waits for
the token window to reset when the last response said too few tokens remain
(``x-ratelimit-*`` headers). 429s are retried with exponential backoff plus
jitter, honouring ``retry-after``. Daily limits (RPD/TPD) raise
``DailyLimitError`` so the caller can stop and resume later.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Self

import httpx

from app.settings import AppSettings

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
DAILY_LIMIT = re.compile(r"per day|\bTPD\b|\bRPD\b", re.IGNORECASE)
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
# Rough uncached tokens a request needs; used only for the throttle.
TOKENS_NEEDED_ESTIMATE = 1500


class LLMError(RuntimeError):
    """The chat API returned an error or an unusable response."""


class RateLimitedError(LLMError):
    """The provider rate-limited the request (HTTP 429) and retries ran out."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class DailyLimitError(RateLimitedError):
    """A per-day request or token limit was reached for a model."""


@dataclass
class Completion:
    """One model response with usage accounting."""

    text: str
    model: str
    prompt_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_s: float


def parse_duration(text: str) -> float:
    """Parse rate-limit durations like '1m26.4s', '674ms', '7.66s'."""
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(n) * scale[u] for n, u in DURATION_PART.findall(text or ""))


@dataclass
class _ModelWindow:
    """Throttle state for one model."""

    lock: asyncio.Lock
    last_start: float = 0.0
    tokens_remaining: int | None = None
    tokens_reset_at: float = 0.0


class ChatClient:
    """Thin wrapper around ``POST /chat/completions``.

    The API key is read from settings and only ever placed in the request
    header; it is never logged or returned.
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._provider = settings.llm_provider
        self._windows: dict[str, _ModelWindow] = {}
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=settings.request_timeout_s,
            headers={"Authorization":
                     f"Bearer {settings.api_key.get_secret_value()}"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    def _payload(
        self, model: str, messages: list[dict[str, Any]], temperature: float,
        max_tokens: int, effort: str, response_format: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        if self._provider == "openrouter":
            payload["usage"] = {"include": True}
            if effort:
                payload["reasoning"] = {"effort": effort, "exclude": True}
            return payload
        # Groq: plain-string content; model-specific reasoning params.
        payload["messages"] = [_flatten(m) for m in messages]
        if model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = effort or "low"
            payload["include_reasoning"] = False
        elif model.startswith("qwen/"):
            payload["reasoning_format"] = "hidden"
        return payload

    async def _throttle(self, model: str) -> None:
        window = self._windows.setdefault(model, _ModelWindow(asyncio.Lock()))
        async with window.lock:
            now = time.monotonic()
            gap = 60.0 / max(self._settings.rate_limit_rpm, 1)
            wait = window.last_start + gap - now
            if (window.tokens_remaining is not None
                    and window.tokens_remaining < TOKENS_NEEDED_ESTIMATE):
                wait = max(wait, window.tokens_reset_at - now)
            if wait > 0:
                await asyncio.sleep(wait)
            window.last_start = time.monotonic()

    def _record_headers(self, model: str, headers: httpx.Headers) -> None:
        window = self._windows[model]
        remaining = headers.get("x-ratelimit-remaining-tokens")
        if remaining is not None:
            window.tokens_remaining = int(float(remaining))
            window.tokens_reset_at = time.monotonic() + parse_duration(
                headers.get("x-ratelimit-reset-tokens", ""))

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        """Send one chat request, throttled and retried on transient errors."""
        s = self._settings
        payload = self._payload(
            model, messages,
            s.temperature if temperature is None else temperature,
            max_tokens or s.max_tokens,
            reasoning_effort or s.reasoning_effort, response_format)
        for attempt in range(1, s.llm_max_retries + 1):
            await self._throttle(model)
            start = time.perf_counter()
            try:
                resp = await self._client.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                if attempt == s.llm_max_retries:
                    raise LLMError(f"{model}: transport error {exc!r}") from exc
                await asyncio.sleep(_backoff(attempt))
                continue
            latency = time.perf_counter() - start
            self._record_headers(model, resp.headers)
            retry_after = resp.headers.get("retry-after")
            if resp.status_code == 429 and DAILY_LIMIT.search(resp.text):
                raise DailyLimitError(f"{model}: daily limit reached: "
                                      f"{resp.text[:200]}",
                                      _seconds(retry_after))
            if resp.status_code in RETRY_STATUS and attempt < s.llm_max_retries:
                delay = (float(retry_after) + random.uniform(0, 1)
                         if retry_after else _backoff(attempt))
                await asyncio.sleep(delay)
                continue
            if resp.status_code == 429:
                raise RateLimitedError(f"{model}: rate limited: "
                                       f"{resp.text[:200]}",
                                       _seconds(retry_after))
            if resp.status_code != 200:
                raise LLMError(f"{model}: HTTP {resp.status_code} "
                               f"{resp.text[:300]}")
            return self._parse(model, resp.json(), latency)
        raise LLMError(f"{model}: retries exhausted")

    def _parse(self, model: str, data: dict[str, Any], latency: float
               ) -> Completion:
        """Turn an API response into a Completion."""
        if "error" in data:
            raise LLMError(f"{model}: {data['error']}")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"{model}: malformed response") from exc
        usage = data.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = int(prompt_details.get("cached_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        # OpenRouter reports cost; Groq doesn't (0.0).
        cost = float(usage.get("cost") or 0.0)
        return Completion(
            text=(message.get("content") or "").strip(),
            model=data.get("model", model),
            prompt_tokens=prompt,
            cached_tokens=cached,
            cache_write_tokens=int(prompt_details.get("cache_write_tokens") or 0),
            completion_tokens=completion,
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
            cost_usd=cost,
            latency_s=latency,
        )


def _flatten(message: dict[str, Any]) -> dict[str, Any]:
    """Join content parts into one string (drops cache_control hints)."""
    content = message.get("content")
    if isinstance(content, list):
        content = "\n\n".join(part.get("text", "") for part in content)
    return {**message, "content": content}


def _seconds(value: str | None) -> float | None:
    """Parse a retry-after header value in seconds (None if absent/bad)."""
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter, capped at 60 s."""
    return random.uniform(0, min(60.0, 2.0 ** attempt))
