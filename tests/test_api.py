"""API tests: auth, validation, guard fallback, sessions, error mapping.

No real API calls: the LLM is the ``StubClient`` (canned answers) or small
stand-ins that raise the client's own exceptions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.pipeline import (
    Client,
    guard_fallback,
    lead_intent,
    unavailable_answer,
)
from app.api.sessions import InMemorySessionStore, Turn
from app.chat.llm import Completion, LLMError, RateLimitedError
from app.chat.prompt import Page
from app.main import create_app
from app.settings import AppSettings
from tests.stub_client import CANNED, FAKE_PRIVATE_IP, StubClient

KEY = "test-key"
HEADERS = {"X-API-Key": KEY}
PAGES = [
    Page(url="https://trendwise.io/pricing", path="/pricing", title="Pricing",
         body="Starter $299/month. Professional $399/month."),
    Page(url="https://trendwise.io/book-demo", path="/book-demo",
         title="Book a demo", body="Book a demo on your own channels."),
]
GOOD, LEAKY = 0, 3  # indexes into StubClient's CANNED answers
Picker = Callable[[int], None]


def settings(**overrides: float | bool) -> AppSettings:
    """Isolated settings (ignore the developer's .env)."""
    return AppSettings(_env_file=None, chat_api_key=KEY,
                       public_base_url="https://trendwise.io",
                       sales_email="sales@trendwise.io", **overrides)


class RecordingStub(StubClient):
    """The stub, also recording the messages it was sent."""

    def __init__(self, daily_limit_after: int | None = None) -> None:
        super().__init__(daily_limit_after=daily_limit_after)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, model: str, messages: list[dict[str, Any]],
                       **kwargs: object) -> Completion:
        """Record, then return the stub's canned answer."""
        self.calls.append(messages)
        return await super().complete(model, messages, **kwargs)


class RaisingClient:
    """Raises a given exception, like the real client on provider failure."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def complete(self, model: str, messages: list[dict[str, Any]],
                       **kwargs: object) -> Completion:
        """Raise the configured exception."""
        raise self.exc


class SlowClient:
    """Never answers in time."""

    async def complete(self, model: str, messages: list[dict[str, Any]],
                       **kwargs: object) -> Completion:
        """Sleep past the API timeout."""
        await asyncio.sleep(5)
        raise AssertionError("unreachable")


def make_client(llm: Client, pages: list[Page] | None = None,
                store: InMemorySessionStore | None = None,
                **overrides: float | bool) -> TestClient:
    """Build the app with injected dependencies."""
    app = create_app(settings=settings(**overrides), client=llm,
                     pages=PAGES if pages is None else pages, store=store)
    return TestClient(app)


@pytest.fixture
def pick(monkeypatch: pytest.MonkeyPatch) -> Iterator[Picker]:
    """Choose which canned stub answer is returned."""
    def _pick(index: int) -> None:
        monkeypatch.setattr("tests.stub_client.random.choice",
                            lambda seq: seq[index])
    yield _pick


def chat(client: TestClient, message: str = "What plans do you offer?",
         session_id: str = "s1", headers: dict[str, str] | None = None
         ) -> httpx.Response:
    """POST /v1/chat."""
    return client.post("/v1/chat", headers=HEADERS if headers is None
                       else headers,
                       json={"session_id": session_id, "message": message})


# ---- health ---------------------------------------------------------------

def test_health_is_open_and_reveals_no_model() -> None:
    """Health needs no key, reports the corpus, and names no model."""
    with make_client(StubClient()) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "corpus_loaded": True,
                           "corpus_pages": 2}
    assert "gpt" not in resp.text.lower()


# ---- API docs -------------------------------------------------------------

def test_docs_disabled_by_default() -> None:
    """Without DOCS_ENABLED, /docs and /openapi.json don't exist."""
    with make_client(StubClient()) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_docs_served_when_enabled() -> None:
    """DOCS_ENABLED=true serves the docs and the schema, still model-free."""
    with make_client(StubClient(), docs_enabled=True) as client:
        docs = client.get("/docs")
        schema = client.get("/openapi.json")
    assert docs.status_code == 200
    assert schema.status_code == 200
    assert "/v1/chat" in schema.json()["paths"]
    assert "gpt" not in schema.text.lower()


def test_openapi_documents_error_responses() -> None:
    """The schema lists every status code the routes can return."""
    with make_client(StubClient(), docs_enabled=True) as client:
        paths = client.get("/openapi.json").json()["paths"]
    chat_codes = set(paths["/v1/chat"]["post"]["responses"])
    delete_codes = set(paths["/v1/chat/{session_id}"]["delete"]["responses"])
    assert {"200", "401", "422", "429", "503"} <= chat_codes
    assert {"204", "401", "422"} <= delete_codes
    assert "Retry-After" in (
        paths["/v1/chat"]["post"]["responses"]["429"]["headers"])


def test_settings_errors_never_echo_secrets(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing setting must not print other loaded values (e.g. keys)."""
    monkeypatch.setenv("GROQ_API_KEY", "SECRET-VALUE-123")
    monkeypatch.delenv("CHAT_API_KEY", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        AppSettings(_env_file=None)
    assert "SECRET-VALUE-123" not in str(exc_info.value)


# ---- auth -----------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"},
                                     {"X-API-Key": ""}])
def test_chat_requires_valid_api_key(headers: dict[str, str]) -> None:
    """Missing, wrong or empty keys get 401 with a request id."""
    with make_client(StubClient()) as client:
        resp = chat(client, headers=headers)
        reset = client.delete("/v1/chat/s1", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"
    assert resp.json()["request_id"] == resp.headers["X-Request-ID"]
    assert reset.status_code == 401


# ---- validation -----------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"session_id": "s1", "message": ""},
    {"session_id": "s1", "message": "   "},
    {"session_id": "s1", "message": "x" * 2001},
    {"session_id": "", "message": "hi"},
    {"session_id": "s" * 129, "message": "hi"},
    {"session_id": "s1"},
    {"message": "hi"},
])
def test_chat_validation_errors(body: dict[str, str]) -> None:
    """Out-of-range or missing fields get 422 without echoing input."""
    with make_client(StubClient()) as client:
        resp = client.post("/v1/chat", headers=HEADERS, json=body)
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"
    assert "x" * 50 not in resp.text


def test_limits_are_inclusive(pick: Picker) -> None:
    """2000-char messages and 128-char session ids are accepted."""
    pick(GOOD)
    with make_client(StubClient()) as client:
        resp = chat(client, message="x" * 2000, session_id="s" * 128)
    assert resp.status_code == 200


def test_delete_validates_session_id_length() -> None:
    """DELETE rejects over-long session ids."""
    with make_client(StubClient()) as client:
        resp = client.delete("/v1/chat/" + "s" * 129, headers=HEADERS)
    assert resp.status_code == 422


# ---- happy path and guard -------------------------------------------------

def test_chat_returns_guarded_answer_with_sources(pick: Picker) -> None:
    """A clean answer passes through with its whitelisted sources."""
    pick(GOOD)
    with make_client(StubClient()) as client:
        resp = chat(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == CANNED[GOOD]
    assert data["sources"] == ["https://trendwise.io/pricing"]
    assert data["lead_intent"] is False
    assert data["session_id"] == "s1"
    assert data["request_id"] == resp.headers["X-Request-ID"]


def test_guard_replaces_leaky_answer(pick: Picker) -> None:
    """An answer with an internal URL and invented trial is replaced."""
    pick(LEAKY)
    with make_client(StubClient()) as client:
        resp = chat(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == guard_fallback(settings())
    assert data["sources"] == []
    assert FAKE_PRIVATE_IP not in resp.text
    assert "14-day" not in resp.text


def test_lead_intent_flag(pick: Picker) -> None:
    """Buying-intent messages set lead_intent."""
    pick(GOOD)
    with make_client(StubClient()) as client:
        resp = chat(client, message="Can I book a demo for my agency?")
    assert resp.json()["lead_intent"] is True


@pytest.mark.parametrize(("message", "expected"), [
    ("do you offer a free trial?", False),
    ("how much is annual?", False),
    ("I want a demo", True),
    ("my email is a@b.com", True),
    # Pricing / feature questions alone stay false.
    ("How much is the Professional plan?", False),
    ("Is there a demo video I can watch?", False),
    ("Which plan fits a team of 8?", False),
    # Other explicit intent.
    ("Can I get a quote for 50 keywords?", True),
    ("I'd like to talk to sales", True),
    ("I'm Sara from Acme Media", True),
])
def test_lead_intent_only_for_explicit_intent(message: str,
                                              expected: bool) -> None:
    """lead_intent is true only for explicit intent or shared details."""
    assert lead_intent(message) is expected


# ---- sessions -------------------------------------------------------------

def test_follow_up_uses_history_until_reset(pick: Picker) -> None:
    """'and yearly?' is sent with the Business turn; not after DELETE."""
    pick(GOOD)
    stub = RecordingStub()
    first = "How much is the Business plan?"
    with make_client(stub) as client:
        chat(client, message=first, session_id="biz")
        chat(client, message="and yearly?", session_id="biz")
        assert client.delete("/v1/chat/biz", headers=HEADERS).status_code == 204
        chat(client, message="and yearly?", session_id="biz")
    follow_up, after_reset = stub.calls[1], stub.calls[2]
    contents = [m["content"] for m in follow_up[1:]]
    assert contents == [first, CANNED[GOOD], "and yearly?"]
    assert all(first not in str(m["content"]) for m in after_reset[1:])
    assert [m["content"] for m in after_reset[1:]] == ["and yearly?"]


def test_history_is_sent_and_reset_clears_it(pick: Picker) -> None:
    """Turns are replayed to the model; DELETE starts a fresh session."""
    pick(GOOD)
    stub = RecordingStub()
    with make_client(stub) as client:
        chat(client, message="first")
        chat(client, message="second")
        assert client.delete("/v1/chat/s1", headers=HEADERS).status_code == 204
        chat(client, message="third")
        # Deleting an unknown session is still 204 (idempotent).
        assert client.delete("/v1/chat/nope", headers=HEADERS).status_code == 204
    second, third = stub.calls[1], stub.calls[2]
    assert [m["content"] for m in second[1:]] == ["first", CANNED[GOOD],
                                                  "second"]
    assert [m["content"] for m in third[1:]] == ["third"]


def test_sessions_are_isolated(pick: Picker) -> None:
    """One session's history never reaches another session's prompt."""
    pick(GOOD)
    stub = RecordingStub()
    with make_client(stub) as client:
        chat(client, message="my email is a@b.example", session_id="a")
        chat(client, message="hello", session_id="b")
    assert all("a@b.example" not in str(m) for m in stub.calls[1])


def test_store_keeps_last_turns_and_expires_idle_sessions() -> None:
    """InMemorySessionStore caps history and drops idle sessions."""
    now = [0.0]
    store = InMemorySessionStore(max_turns=10, ttl_s=1800, clock=lambda: now[0])

    async def scenario() -> tuple[list[Turn], list[Turn]]:
        for i in range(12):
            await store.append_turn("s", Turn(f"q{i}", f"a{i}"))
        kept = await store.get_turns("s")
        now[0] += 1801
        return kept, await store.get_turns("s")

    kept, expired = asyncio.run(scenario())
    assert [t.user for t in kept] == [f"q{i}" for i in range(2, 12)]
    assert expired == []


# ---- upstream failures ----------------------------------------------------

def test_llm_failure_returns_503_without_internals() -> None:
    """Provider errors become a safe 503; no provider detail leaks."""
    exc = LLMError(f"openai/gpt-oss-20b: HTTP 500 from {FAKE_PRIVATE_IP}: boom")
    with make_client(RaisingClient(exc)) as client:
        resp = chat(client)
    assert resp.status_code == 503
    assert resp.json()["error"] == "unavailable"
    assert resp.json()["answer"] == unavailable_answer(settings())
    for secret in ("gpt-oss", FAKE_PRIVATE_IP, "HTTP 500", "boom", "Traceback"):
        assert secret not in resp.text


def test_timeout_returns_503() -> None:
    """A model that doesn't answer in time gives 503."""
    with make_client(SlowClient(), chat_timeout_s=0.05) as client:
        resp = chat(client)
    assert resp.status_code == 503


def test_provider_rate_limit_returns_429_with_retry_after() -> None:
    """The provider's retry-after is passed on, rounded up."""
    exc = RateLimitedError("groq: TPM limit", retry_after_s=12.3)
    with make_client(RaisingClient(exc)) as client:
        resp = chat(client)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "13"
    assert resp.json()["answer"] == unavailable_answer(settings())
    assert "TPM" not in resp.text


def test_daily_limit_from_stub_returns_429_default_retry_after() -> None:
    """The stub's simulated daily limit maps to 429 + default Retry-After."""
    with make_client(StubClient(daily_limit_after=0)) as client:
        resp = chat(client)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "30"


def test_missing_corpus_reports_unhealthy_and_503() -> None:
    """Without a corpus the service stays up but refuses to answer."""
    with make_client(StubClient(), pages=[]) as client:
        health = client.get("/health").json()
        resp = chat(client)
    assert health["corpus_loaded"] is False
    assert resp.status_code == 503


def test_failed_turn_is_not_saved() -> None:
    """A failed call leaves history unchanged."""
    store = InMemorySessionStore(max_turns=10, ttl_s=1800)
    with make_client(RaisingClient(LLMError("x")), store=store) as client:
        chat(client)
    assert asyncio.run(store.get_turns("s1")) == []
