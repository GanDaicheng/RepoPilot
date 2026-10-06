"""Central redacted HTTP error mapping."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from repopilot.services.task_service import TaskServiceError


async def task_service_error_handler(
    request: Request,
    exc: TaskServiceError,
) -> JSONResponse:
    del request
    status_code = {
        "task_not_found": 404,
        "task_conflict": 409,
    }.get(exc.error_code, 422)
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": exc.error_code, "message": str(exc)}},
    )


async def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del request, exc
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": "The request body is invalid.",
            }
        },
    )


async def unknown_error_handler(request: Request, exc: Exception) -> JSONResponse:
    del request, exc
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "internal_error",
                "message": "The request could not be completed.",
            }
        },
    )
