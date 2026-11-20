from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.pipeline.character_task_models import TaskError, TaskId, TaskRequest, TaskState
from app.pipeline.character_tasks import CharacterTasks, TaskConflict
from app.storage.shared_files import InvalidSourceReference, SourceVideoNotFound

router = APIRouter(prefix="/internal/v1/character-extraction/tasks")


def task_service(request: Request) -> CharacterTasks:
    return request.app.state.character_tasks


def task_http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=TaskError(code=code, message=message).model_dump(),
    )


@router.post("", status_code=202)
def submit_task(
    payload: TaskRequest,
    request: Request,
    service: Annotated[CharacterTasks, Depends(task_service)],
) -> TaskState:
    request.state.task_id = payload.task_id
    try:
        return service.submit(payload)
    except InvalidSourceReference as exc:
        raise task_http_error(422, "SOURCE_VIDEO_REF_INVALID", str(exc)) from exc
    except SourceVideoNotFound as exc:
        raise task_http_error(404, "SOURCE_VIDEO_NOT_FOUND", str(exc)) from exc
    except TaskConflict as exc:
        raise task_http_error(409, "TASK_CONFLICT", str(exc)) from exc


@router.get("/{task_id}")
def get_task(
    task_id: TaskId,
    request: Request,
    service: Annotated[CharacterTasks, Depends(task_service)],
) -> TaskState:
    request.state.task_id = task_id
    state = service.get(task_id)
    if state is None:
        raise task_http_error(404, "TASK_NOT_FOUND", "找不到人物提取任务")
    return state
