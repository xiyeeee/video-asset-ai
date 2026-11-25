import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.task_errors import task_http_error
from app.live2d.motion_capabilities import read_capabilities
from app.live2d.motion_models import Capabilities, ModelRef, MotionError, MotionRequest
from app.pipeline.character_task_models import TaskId
from app.pipeline.character_tasks import TaskConflict
from app.pipeline.live2d_motion_tasks import Live2DMotionTasks, MotionState
from app.storage.shared_files import InvalidSourceReference

router = APIRouter(prefix="/internal/v1/live2d")
logger = logging.getLogger(__name__)


def motion_service(request: Request) -> Live2DMotionTasks:
    return request.app.state.live2d_motion_tasks


MotionService = Annotated[Live2DMotionTasks, Depends(motion_service)]


@router.post("/motion-capabilities")
def capabilities(
    payload: ModelRef, request: Request, service: MotionService
) -> Capabilities:
    request.state.model_version_id = payload.model_version_id
    try:
        return read_capabilities(payload, service.settings)
    except InvalidSourceReference as exc:
        raise task_http_error(
            422, "MODEL_REFERENCE_INVALID", "模型入口引用无效"
        ) from exc
    except FileNotFoundError as exc:
        raise task_http_error(404, "MODEL_NOT_FOUND", "找不到模型版本入口") from exc
    except MotionError as exc:
        logger.exception(
            "modelVersionId=%s stage=INSPECTING_MODEL code=%s",
            payload.model_version_id,
            exc.code,
        )
        statuses = {
            "MODEL_VERSION_MISMATCH": 409,
            "CUBISM_CORE_UNAVAILABLE": 503,
            "MODEL_INSPECTION_TIMEOUT": 504,
        }
        raise task_http_error(statuses.get(exc.code, 422), exc.code, str(exc)) from exc


@router.post("/motion-tasks", status_code=202)
def submit_motion(
    payload: MotionRequest, request: Request, service: MotionService
) -> MotionState:
    request.state.task_id = payload.task_id
    request.state.model_version_id = payload.model_version_id
    try:
        return service.submit(payload)
    except TaskConflict as exc:
        raise task_http_error(409, "TASK_CONFLICT", str(exc)) from exc


@router.get("/motion-tasks/{task_id}")
def motion_status(
    task_id: TaskId, request: Request, service: MotionService
) -> MotionState:
    request.state.task_id = task_id
    state = service.get(task_id)
    if state is None:
        raise task_http_error(404, "TASK_NOT_FOUND", "找不到组合动作任务")
    request.state.model_version_id = state.model_version_id
    return state
