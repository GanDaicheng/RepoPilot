"""REST endpoints for persistent RepoPilot tasks."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from repopilot.api.dependencies import AppRuntime
from repopilot.api.schemas import HealthResponse, TaskCreateRequest, TaskResponse


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
