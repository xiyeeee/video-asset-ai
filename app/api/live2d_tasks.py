from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.task_errors import task_http_error
from app.pipeline.character_task_models import TaskId
from app.pipeline.character_tasks import TaskConflict
from app.pipeline.live2d_tasks import Live2DRequest, Live2DState, Live2DTasks
from app.storage.shared_files import InvalidSourceReference

router = APIRouter(prefix="/internal/v1/live2d/tasks")


def task_service(request: Request) -> Live2DTasks:
    return request.app.state.live2d_tasks


@router.post("", status_code=202)
def submit_task(
    payload: Live2DRequest,
    request: Request,
    service: Annotated[Live2DTasks, Depends(task_service)],
) -> Live2DState:
    request.state.task_id = payload.task_id
    try:
        return service.submit(payload)
    except InvalidSourceReference as exc:
        raise task_http_error(422, "SOURCE_IMAGE_REF_INVALID", str(exc)) from exc
    except FileNotFoundError as exc:
        raise task_http_error(
            404, "SOURCE_IMAGE_NOT_FOUND", "共享输入目录中找不到源图片"
        ) from exc
    except TaskConflict as exc:
        raise task_http_error(409, "TASK_CONFLICT", str(exc)) from exc


@router.get("/{task_id}")
def get_task(
    task_id: TaskId,
    request: Request,
    service: Annotated[Live2DTasks, Depends(task_service)],
) -> Live2DState:
    request.state.task_id = task_id
    state = service.get(task_id)
    if state is None:
        raise task_http_error(404, "TASK_NOT_FOUND", "找不到 Live2D 任务")
    return state
