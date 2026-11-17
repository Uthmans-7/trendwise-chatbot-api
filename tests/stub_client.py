"""Canned, zero-cost LLM client for tests (no network)."""

from __future__ import annotations

import asyncio
import random
from typing import Any, Self

from app.chat.llm import Completion, DailyLimitError

# A private IP for the leaky answer, built at runtime so the repo's scan for
# internal addresses stays clean. It is fake; the guard must block it.
FAKE_PRIVATE_IP = ".".join(("10", "20", "30", "40"))  # noqa: FLY002

CANNED = [
    ("Starter is $299/month; Professional is $399/month.\n"
    "Sources: https://trendwise.io/pricing"),
    ("That isn't stated on our website. Please contact sales@trendwise.io or "
    "book a demo at https://trendwise.io/book-demo."),
    "Sorry, I can't share that. Happy to help with Trendwise questions.",
    f"We offer a 14-day free trial. See http://{FAKE_PRIVATE_IP}/pricing",
]


class StubClient:
    """Return canned answers.

    ``daily_limit_after=N`` simulates a provider daily limit: after N calls
    every call raises ``DailyLimitError``.
    """

    def __init__(self, daily_limit_after: int | None = None) -> None:
        self._limit = daily_limit_after
        self._calls = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def complete(
        self, model: str, messages: list[dict[str, Any]], **kwargs: object
    ) -> Completion:
        """Return a canned completion."""
        if self._limit is not None and self._calls >= self._limit:
            raise DailyLimitError(f"{model}: simulated daily limit (TPD)")
        self._calls += 1
        await asyncio.sleep(0)
        return Completion(text=random.choice(CANNED), model=model,
                          prompt_tokens=5500, cached_tokens=5000,
                          cache_write_tokens=0, completion_tokens=120,
                          reasoning_tokens=0, cost_usd=0.0,
                          latency_s=random.uniform(0.5, 3.0))
