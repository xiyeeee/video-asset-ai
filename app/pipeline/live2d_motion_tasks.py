import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Condition
from time import perf_counter
from typing import Literal

from pydantic import Field

from app.core.config import Settings
from app.live2d.compile_motion import MAX_SAVED_BYTES, compile_motion
from app.live2d.motion_capabilities import read_capabilities
from app.live2d.motion_models import (
    Contract,
    Diagnostic,
    MotionError,
    MotionRequest,
    VersionId,
)
from app.pipeline.character_task_models import TaskId, TaskStatus
from app.pipeline.character_tasks import TaskConflict
from app.storage.shared_files import (
    InvalidSourceReference,
    publish_task_directory,
    stage_task_directory,
)

logger = logging.getLogger(__name__)
Stage = Literal["INSPECTING_MODEL", "COMPILING", "PUBLISHING"]


class MotionResult(Contract):
    format_version: Literal[1] = 1
    format: Literal["cubism-motion3"] = "cubism-motion3"
    duration_ms: int
    artifact_ref: str
    artifact_sha256: str
    model_moc_sha256: str


class MotionFailure(Contract):
    code: str
    message: str
    stage: Stage


class MotionState(Contract):
    task_id: TaskId
    model_version_id: VersionId
    status: TaskStatus = TaskStatus.QUEUED
    stage: Stage | None = None
    result: MotionResult | None = None
    error: MotionFailure | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


@dataclass
class MotionEntry:
    request: MotionRequest
    state: MotionState


class Live2DMotionTasks:
    def __init__(self, settings: Settings, executor: ThreadPoolExecutor) -> None:
        self.settings = settings
        self.executor = executor
        self.output_root = settings.live2d_output_root.resolve() / "motion-tasks"
        self.entries: dict[str, MotionEntry] = {}
        self.lock = Condition()

    def submit(self, request: MotionRequest) -> MotionState:
        with self.lock:
            existing = self.entries.get(request.task_id)
            if existing:
                if existing.request != request:
                    raise TaskConflict("该 taskId 已绑定其他组合动作参数")
                return existing.state
            state = MotionState(
                task_id=request.task_id, model_version_id=request.model_version_id
            )
            self.entries[request.task_id] = MotionEntry(request, state)
            try:
                self.executor.submit(self.execute, request)
            except RuntimeError:
                del self.entries[request.task_id]
                raise
        return state

    def get(self, task_id: str) -> MotionState | None:
        with self.lock:
            entry = self.entries.get(task_id)
            return entry.state if entry else None

    def wait(self, task_id: str, timeout: float) -> MotionState | None:
        with self.lock:
            finished = self.lock.wait_for(
                lambda: (
                    self.entries[task_id].state.status
                    in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}
                ),
                timeout=timeout,
            )
            return self.entries[task_id].state if finished else None

    def execute(self, request: MotionRequest) -> None:
        stage: Stage = "INSPECTING_MODEL"
        started = perf_counter()

        def save(
            status: TaskStatus,
            *,
            result: MotionResult | None = None,
            error: MotionFailure | None = None,
            diagnostics: list[Diagnostic] | None = None,
        ) -> None:
            with self.lock:
                self.entries[request.task_id].state = MotionState(
                    task_id=request.task_id,
                    model_version_id=request.model_version_id,
                    status=status,
                    stage=stage,
                    result=result,
                    error=error,
                    diagnostics=diagnostics or [],
                )
                self.lock.notify_all()

        def advance(next_stage: Stage) -> None:
            nonlocal stage
            stage = next_stage
            save(TaskStatus.RUNNING)
            logger.info(
                "taskId=%s modelVersionId=%s stage=%s",
                request.task_id,
                request.model_version_id,
                stage,
            )

        try:
            advance("INSPECTING_MODEL")
            try:
                model = read_capabilities(request, self.settings)
            except (FileNotFoundError, InvalidSourceReference) as exc:
                raise MotionError(
                    "MODEL_REFERENCE_INVALID", "找不到模型版本入口或引用无效"
                ) from exc
            advance("COMPILING")
            motion = compile_motion(request, model, self.settings)
            payload = (
                json.dumps(motion, separators=(",", ":"), allow_nan=False) + "\n"
            ).encode()
            if len(payload) > MAX_SAVED_BYTES:
                raise MotionError("MOTION_TOO_LARGE", "编译结果超出动作文件大小限制")
            advance("PUBLISHING")
            digest = hashlib.sha256(payload).hexdigest()
            name = f"{request.task_id}-{digest}"
            destination = self.output_root / name
            artifact = destination / "combo.motion3.json"
            try:
                # 结果按内容命名。重启重算时复用同一文件，已引用的版本可以继续读取。
                if destination.exists():
                    if (
                        artifact.resolve() != artifact
                        or artifact.read_bytes() != payload
                    ):
                        raise ValueError("已发布的动作内容与摘要不符")
                else:
                    with stage_task_directory(
                        self.output_root, prefix=".motion-"
                    ) as temp:
                        staged = self.output_root / temp / "artifact"
                        staged.mkdir()
                        (staged / artifact.name).write_bytes(payload)
                        publish_task_directory(self.output_root, staged, destination)
            except (OSError, ValueError) as exc:
                raise MotionError(
                    "MOTION_PUBLISH_FAILED", "组合动作文件发布失败"
                ) from exc
            save(
                TaskStatus.SUCCEEDED,
                result=MotionResult(
                    duration_ms=request.duration_ms,
                    artifact_ref=artifact.relative_to(
                        self.output_root.parent
                    ).as_posix(),
                    artifact_sha256=digest,
                    model_moc_sha256=model.moc_sha256,
                ),
            )
        except Exception as exc:
            code, message = ("MOTION_TASK_FAILED", "组合动作任务失败")
            diagnostics: list[Diagnostic] = []
            if isinstance(exc, MotionError):
                code, message, diagnostics = exc.code, str(exc), exc.diagnostics
            logger.exception(
                "taskId=%s modelVersionId=%s stage=%s code=%s",
                request.task_id,
                request.model_version_id,
                stage,
                code,
            )
            save(
                TaskStatus.FAILED,
                error=MotionFailure(code=code, message=message, stage=stage),
                diagnostics=diagnostics,
            )
        finally:
            logger.info(
                "taskId=%s modelVersionId=%s stage=%s 耗时 %.2fs",
                request.task_id,
                request.model_version_id,
                stage,
                perf_counter() - started,
            )
