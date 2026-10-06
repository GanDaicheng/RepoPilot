"""FastAPI application factory with managed database, saver, and Worker lifecycle."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from repopilot.api.dependencies import (
    AppOverrides,
    AppSettings,
    assemble_runtime,
    default_checkpointer_factory,
)
from repopilot.api.errors import (
    task_service_error_handler,
    unknown_error_handler,
    validation_error_handler,
)
from repopilot.api.routes import router
from repopilot.services.task_service import TaskServiceError


MAX_REQUEST_BODY_BYTES = 65_536


class RequestSizeLimitMiddleware:
    """Reject oversized bodies while reading ASGI chunks, before validation buffers them."""

    def __init__(self, app, max_body_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        raw_length = headers.get(b"content-length")
        try:
            declared_too_large = (
                raw_length is not None and int(raw_length) > self.max_body_bytes
            )
        except ValueError:
            declared_too_large = True
        if declared_too_large:
            await self._reject(scope, receive, send)
            return

        buffered: list[dict[str, object]] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if total > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            more_body = bool(message.get("more_body", False))

        async def replay_receive():
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "request_too_large",
                    "message": "The request body exceeds the supported size limit.",
                }
            },
        )
        await response(scope, receive, send)


def create_app(
    settings: AppSettings | None = None,
    *,
    overrides: AppOverrides | None = None,
) -> FastAPI:
    configured = settings or AppSettings()
    injected = overrides or AppOverrides()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
        configured.data_dir.mkdir(parents=True, exist_ok=True)
        factory = injected.checkpointer_factory or default_checkpointer_factory
        async with factory(str(configured.checkpoint_path)) as checkpointer:
            setup = getattr(checkpointer, "setup", None)
            if setup is not None:
                await setup()
            runtime = await assemble_runtime(configured, injected, checkpointer)
            app.state.runtime = runtime
            runtime.worker.start()
            try:
                yield
            finally:
                await runtime.worker.stop()

    app = FastAPI(title="RepoPilot", version="0.2.0", lifespan=lifespan)

    app.add_middleware(RequestSizeLimitMiddleware)
    app.include_router(router)
    app.add_exception_handler(TaskServiceError, task_service_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unknown_error_handler)
    return app
