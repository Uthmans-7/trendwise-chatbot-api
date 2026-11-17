"""Request, response and error bodies, plus their OpenAPI response maps."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_MESSAGE_CHARS = 2000
MAX_SESSION_ID_CHARS = 128


class ChatRequest(BaseModel):
    """Body of ``POST /v1/chat``."""

    session_id: str = Field(min_length=1, max_length=MAX_SESSION_ID_CHARS)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("message", "session_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ChatResponse(BaseModel):
    """Successful ``POST /v1/chat`` response.

    Sources and lead intent are computed and logged, not returned; the
    request id is sent in the ``X-Request-ID`` header.
    """

    session_id: str
    answer: str


class ErrorBody(BaseModel):
    """Body of every error response."""

    error: str
    message: str
    request_id: str


class FieldError(BaseModel):
    """One invalid request field."""

    field: str
    message: str


class ValidationErrorBody(ErrorBody):
    """422 body: which fields are invalid (input is never echoed)."""

    details: list[FieldError]


class ChatErrorBody(ErrorBody):
    """429/503 body for /v1/chat, with a safe answer to show the visitor."""

    session_id: str
    answer: str


RETRY_AFTER_HEADER = {"Retry-After": {
    "description": "Seconds to wait before retrying.",
    "schema": {"type": "integer"}}}
AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorBody, "description": "Missing or invalid X-API-Key."},
    422: {"model": ValidationErrorBody,
          "description": "Invalid request (field missing, blank or too long)."},
}
CHAT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    429: {"model": ChatErrorBody, "headers": RETRY_AFTER_HEADER,
          "description": "Model provider rate limit; retry after Retry-After."},
    503: {"model": ChatErrorBody,
          "description": "Model error or timeout, or no corpus loaded."},
}
DELETE_RESPONSES: dict[int | str, dict[str, Any]] = {
    204: {"description": "History cleared (also if the session didn't exist)."},
    **AUTH_RESPONSES,
}
