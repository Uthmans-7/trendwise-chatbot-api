"""FastAPI service exposing the chat pipeline (v1, buffered responses).

Endpoints: ``POST /v1/chat``, ``DELETE /v1/chat/{session_id}``,
``GET /health``. See ``docs/API.md`` for the contract.

Run: ``uv run uvicorn --factory app.main:create_app --port 8000
--no-server-header``
(a factory, so importing this module never needs secrets).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path as FsPath
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, Header, Path, Request, Response

from app.api.errors import ApiError, add_error_handlers
from app.api.pipeline import ChatPipeline, Client, unavailable_answer
from app.api.schemas import (
    CHAT_RESPONSES,
    DELETE_RESPONSES,
    MAX_SESSION_ID_CHARS,
    ChatRequest,
    ChatResponse,
)
from app.api.sessions import InMemorySessionStore, SessionStore, Turn
from app.chat.llm import ChatClient, LLMError, RateLimitedError
from app.chat.prompt import Page, load_pages
from app.settings import AppSettings

log = logging.getLogger("chat_api")


class Tracer(Protocol):
    """Hook for per-turn tracing (Langfuse later). Must never raise."""

    def record(self, event: dict[str, Any]) -> None:
        """Receive one turn's metadata (no secrets, no message text)."""
        ...


class NoopTracer:
    """Default tracer: does nothing."""

    def record(self, event: dict[str, Any]) -> None:
        """Ignore the event."""


def _log(event: dict[str, Any]) -> None:
    log.info(json.dumps(event, ensure_ascii=False))


def _safe_load_pages(corpus_dir: FsPath) -> list[Page]:
    try:
        return load_pages(corpus_dir)
    except (OSError, KeyError, ValueError):
        log.exception("corpus failed to load")
        return []


def create_app(
    settings: AppSettings | None = None,
    client: Client | None = None,
    store: SessionStore | None = None,
    pages: list[Page] | None = None,
    tracer: Tracer | None = None,
) -> FastAPI:
    """Build the app. Tests inject a stub client, store and pages."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    # httpx logs every upstream URL at INFO; keep logs to our own events.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = settings or AppSettings()
    corpus = _safe_load_pages(cfg.corpus_dir) if pages is None else pages
    sessions = store or InMemorySessionStore(cfg.session_max_turns,
                                             cfg.session_ttl_s)
    trace = tracer or NoopTracer()
    fallback = unavailable_answer(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if client is not None:
            app.state.pipeline = (ChatPipeline(cfg, client, corpus,
                                               cfg.chat_model)
                                  if corpus else None)
            yield
            return
        async with ChatClient(cfg) as real_client:
            app.state.pipeline = (ChatPipeline(cfg, real_client, corpus,
                                               cfg.chat_model)
                                  if corpus else None)
            yield

    app = FastAPI(title="Trendwise chat API", version="1.0.0",
                  lifespan=lifespan, redoc_url=None,
                  docs_url="/docs" if cfg.docs_enabled else None,
                  openapi_url="/openapi.json" if cfg.docs_enabled else None)
    add_error_handlers(app)

    @app.middleware("http")
    async def request_id_mw(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    async def require_api_key(
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = cfg.chat_api_key.get_secret_value().encode()
        if x_api_key is None or not secrets.compare_digest(
                x_api_key.encode(), expected):
            raise ApiError(401, "unauthorized", "Missing or invalid API key.")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "corpus_loaded": bool(corpus),
                "corpus_pages": len(corpus)}

    @app.post("/v1/chat", response_model=ChatResponse,
              responses=CHAT_RESPONSES,
              dependencies=[Depends(require_api_key)])
    async def chat(body: ChatRequest, request: Request) -> ChatResponse:
        request_id = request.state.request_id
        started = time.perf_counter()
        event: dict[str, Any] = {"event": "chat", "request_id": request_id,
                                 "session_id": body.session_id,
                                 "message_chars": len(body.message)}
        safe = {"session_id": body.session_id, "answer": fallback}
        pipeline: ChatPipeline | None = request.app.state.pipeline
        try:
            if pipeline is None:
                raise ApiError(503, "unavailable",
                               "The assistant is unavailable.", safe)
            history = await sessions.get_turns(body.session_id)
            try:
                result = await asyncio.wait_for(
                    pipeline.answer(history, body.message), cfg.chat_timeout_s)
            except RateLimitedError as exc:
                wait = math.ceil(exc.retry_after_s or cfg.default_retry_after_s)
                event["error"] = type(exc).__name__
                raise ApiError(429, "rate_limited",
                               "Too many requests. Please retry later.", safe,
                               {"Retry-After": str(wait)}) from exc
            except (LLMError, TimeoutError) as exc:
                event["error"] = type(exc).__name__
                raise ApiError(503, "unavailable",
                               "The assistant is unavailable.", safe) from exc
            await sessions.append_turn(body.session_id,
                                       Turn(body.message, result.answer))
            usage = result.usage
            event.update({
                "status": 200, "history_turns": len(history),
                "guard_blocked": result.guard_blocked,
                "guard_violations": result.guard_violation_types,
                "lead_intent": result.lead_intent,
                # Whitelisted public page URLs only (guard-checked).
                "sources": result.sources,
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "cached_tokens": usage.cached_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
            })
            return ChatResponse(session_id=body.session_id,
                                answer=result.answer)
        except ApiError as exc:
            event["status"] = exc.status
            raise
        finally:
            event["latency_ms"] = round((time.perf_counter() - started) * 1000)
            _log(event)
            trace.record(event)

    @app.delete("/v1/chat/{session_id}", status_code=204,
                responses=DELETE_RESPONSES,
                dependencies=[Depends(require_api_key)])
    async def reset(
        session_id: Annotated[str, Path(min_length=1,
                                        max_length=MAX_SESSION_ID_CHARS)],
        request: Request,
    ) -> Response:
        existed = await sessions.clear(session_id)
        _log({"event": "reset", "request_id": request.state.request_id,
              "session_id": session_id, "existed": existed})
        return Response(status_code=204)

    return app
