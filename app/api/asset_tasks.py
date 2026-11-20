from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.task_errors import task_http_error
from app.pipeline.asset_task_models import AssetTaskRequest, AssetTaskState
from app.pipeline.asset_tasks import AssetTasks
from app.pipeline.character_task_models import TaskId
from app.pipeline.character_tasks import TaskConflict
from app.storage.shared_files import InvalidSourceReference, SourceVideoNotFound

router = APIRouter(prefix="/internal/v1/asset-extraction/tasks")


def asset_service(request: Request) -> AssetTasks:
    return request.app.state.asset_tasks


@router.post("", status_code=202)
def submit_task(
    payload: AssetTaskRequest,
    request: Request,
    service: Annotated[AssetTasks, Depends(asset_service)],
) -> AssetTaskState:
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
    service: Annotated[AssetTasks, Depends(asset_service)],
) -> AssetTaskState:
    request.state.task_id = task_id
    state = service.get(task_id)
    if state is None:
        raise task_http_error(404, "TASK_NOT_FOUND", "找不到素材提取任务")
    return state
