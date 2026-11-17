"""Run one chat turn for the API: prompt -> LLM -> output guard.

Messages are the static system message first, then history, then the new
visitor message; ``check_answer`` is applied to every answer, and the
guarded answer is what goes into history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.api.sessions import Turn
from app.chat.guard import check_answer
from app.chat.llm import Completion
from app.chat.prompt import Page, system_message
from app.settings import AppSettings

# Explicit lead intent only (the prompt handles the lead conversation; this
# flag tells the backend a lead is forming). Pricing or feature questions on
# their own ("how much is annual?", "free trial?") must NOT match.
LEAD_INTENT_PATTERNS = [
    # Asking for a demo: "I want a demo", "book a demo", "demo please".
    (r"\b(book|schedule|arrange|get|want|need|like|request|set up)\b"
    r"[^.?!\n]{0,40}\bdemo\b"),
    r"\bdemo,? please\b",
    # Asking for a quote.
    r"\bquote (for|on)\b",
    r"\b(a|an|my|the|get|need|want|request) (\w+ )?quote\b",
    # Asking to be contacted or to talk to sales / a person.
    r"\b(contact|call|email|e-mail|reach|ring) me\b",
    r"\bget in touch\b",
    r"\bcontact (sales|your (sales )?team|someone)\b",
    (r"\b(talk|speak|chat) (to|with) (sales|someone|somebody|"
    r"a (real )?(person|human)|an? (agent|rep|representative|salesperson)|"
    r"your (sales )?team)\b"),
    # Visitor providing contact details.
    r"[\w.+-]+@[\w-]+(\.[\w-]+)+",
    r"\bmy name is\b",
    r"\b(my|our) (work )?email\b",
    r"\b(my|our) company\b",
    r"\bI work (at|for)\b",
    r"\bI'?m \w+ from\b",
]
LEAD_INTENT = re.compile("|".join(f"(?:{p})" for p in LEAD_INTENT_PATTERNS),
                         re.IGNORECASE)


class Client(Protocol):
    """What the pipeline needs from an LLM client (real or stub)."""

    async def complete(
        self, model: str, messages: list[dict[str, Any]], **kwargs: object
    ) -> Completion:
        """Return one completion."""
        ...


@dataclass
class ChatResult:
    """Outcome of one chat turn."""

    answer: str
    sources: list[str]
    lead_intent: bool
    guard_blocked: bool
    guard_violation_types: list[str] = field(default_factory=list)
    usage: Completion | None = None


def guard_fallback(settings: AppSettings) -> str:
    """Safe reply used when the output guard blocks an answer."""
    return (f"Sorry, I can't help with that here. For anything not covered "
            f"on our website, please contact {settings.sales_email} or book "
            f"a demo at {settings.public_base_url.rstrip('/')}/book-demo.")


def unavailable_answer(settings: AppSettings) -> str:
    """Safe reply used when the model can't be reached."""
    return (f"Sorry, I can't answer right now. Please contact "
            f"{settings.sales_email} or book a demo at "
            f"{settings.public_base_url.rstrip('/')}/book-demo.")


def lead_intent(message: str) -> bool:
    """Return True only for explicit lead intent.

    That is: asking for a demo, a quote, to be contacted or to talk to
    sales/someone, or the visitor sharing an email, name or company.
    """
    return bool(LEAD_INTENT.search(message))


class ChatPipeline:
    """History + message -> guarded answer, using the existing components."""

    def __init__(self, settings: AppSettings, client: Client,
                 pages: list[Page], model: str) -> None:
        self._client = client
        self._pages = pages
        self._model = model
        self._system = system_message(settings, pages)
        self._fallback = guard_fallback(settings)

    async def answer(self, history: list[Turn], message: str) -> ChatResult:
        """Run one turn. LLM errors propagate to the caller."""
        messages: list[dict[str, Any]] = [self._system]
        for turn in history:
            messages += [{"role": "user", "content": turn.user},
                         {"role": "assistant", "content": turn.assistant}]
        messages.append({"role": "user", "content": message})
        completion = await self._client.complete(self._model, messages)

        visitor_text = "\n".join([*(t.user for t in history), message])
        guard = check_answer(completion.text, self._pages, visitor_text,
                             self._fallback)
        return ChatResult(
            answer=guard.text,
            sources=[] if guard.blocked else guard.cited_urls,
            lead_intent=lead_intent(message),
            guard_blocked=guard.blocked,
            # Types only ("pattern:private_ip", "url", "number"): never the
            # offending value, which could be an internal host.
            guard_violation_types=sorted(
                {v if v.startswith("pattern:") else v.split(":", 1)[0]
                 for v in guard.violations}),
            usage=completion,
        )
