"""FastAPI application factory with managed database, saver, and Worker lifecycle."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
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

    @app.middleware("http")
    async def reject_oversized_requests(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > 65_536
            except ValueError:
                too_large = True
            if too_large:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={
                        "error": {
                            "code": "request_too_large",
                            "message": "The request body exceeds the supported size limit.",
                        }
                    },
                )
        return await call_next(request)

    app.include_router(router)
    app.add_exception_handler(TaskServiceError, task_service_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unknown_error_handler)
    return app
