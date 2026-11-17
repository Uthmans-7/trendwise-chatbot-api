"""Safe error responses: fixed bodies, never internals or input echoes."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("chat_api")


class ApiError(Exception):
    """An error returned to the client with a fixed, safe body."""

    def __init__(self, status: int, code: str, message: str,
                 extra: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message
        self.extra = extra or {}
        self.headers = headers or {}


def error_response(request: Request, exc: ApiError) -> JSONResponse:
    """Render an ``ApiError`` with the request id."""
    body = {"error": exc.code, "message": exc.message,
            "request_id": request.state.request_id, **exc.extra}
    return JSONResponse(body, status_code=exc.status, headers=exc.headers)


def add_error_handlers(app: FastAPI) -> None:
    """Register handlers for API errors, validation and anything unhandled."""

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError
                                ) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request,
                                 exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only; never echo the input back.
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]),
                    "message": e["msg"]} for e in exc.errors()]
        return error_response(request, ApiError(
            422, "validation_error", "Invalid request.", {"details": details}))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception
                                ) -> JSONResponse:
        log.error(json.dumps({"event": "unhandled_error",
                              "request_id": request.state.request_id,
                              "error": type(exc).__name__}))
        return error_response(request, ApiError(
            500, "internal_error", "Something went wrong."))
