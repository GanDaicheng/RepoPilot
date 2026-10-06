"""REST endpoints for persistent RepoPilot tasks."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import StreamingResponse

from repopilot.api.dependencies import AppRuntime
from repopilot.api.schemas import HealthResponse, TaskCreateRequest, TaskResponse
from repopilot.api.sse import event_stream, parse_last_event_id
from repopilot.services.task_service import TaskServiceError


router = APIRouter()


def _runtime(request: Request) -> AppRuntime:
    return request.app.state.runtime


@router.post(
    "/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_task(body: TaskCreateRequest, request: Request) -> TaskResponse:
    task = await _runtime(request).task_service.create_task(body.to_domain())
    return TaskResponse.from_record(task)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, request: Request) -> TaskResponse:
    task = await _runtime(request).task_service.get_task(task_id)
    return TaskResponse.from_record(task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: str, request: Request) -> TaskResponse:
    task = await _runtime(request).task_service.cancel_task(task_id)
    return TaskResponse.from_record(task)


@router.get("/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    request: Request,
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID"),
    ] = None,
) -> StreamingResponse:
    runtime = _runtime(request)
    await runtime.task_service.get_task(task_id)
    try:
        cursor = parse_last_event_id(last_event_id)
    except ValueError:
        raise TaskServiceError(
            "validation_error",
            "Last-Event-ID must be a supported non-negative integer.",
        ) from None
    return StreamingResponse(
        event_stream(
            task_id,
            cursor,
            runtime.event_repository,
            task_repository=runtime.task_repository,
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/health", response_model=HealthResponse)
async def health(request: Request, response: Response) -> HealthResponse:
    runtime = _runtime(request)
    worker_state = "running" if runtime.worker.running else "stopped"
    if worker_state != "running":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        service="ready",
        database="ready",
        worker=worker_state,
    )
