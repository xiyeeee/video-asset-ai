import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import Settings
from app.live2d.generate import GenerationError, Stage, generate_model
from app.pipeline.character_task_models import TaskError, TaskId, TaskStatus
from app.pipeline.character_tasks import TaskConflict
from app.storage.shared_files import (
    publish_task_directory,
    resolve_source_file,
    stage_task_directory,
)

logger = logging.getLogger(__name__)


class Live2DRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    task_id: TaskId = Field(alias="taskId")
    project_id: int | TaskId = Field(alias="projectId")
    model_name: str = Field(
        alias="modelName", min_length=1, max_length=255, pattern=r"\S"
    )
    source_image_ref: str = Field(alias="sourceImageRef", min_length=1, max_length=4096)

    @field_validator("model_name")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("modelName 不能包含 NUL 字符")
        return value


class Live2DResult(BaseModel, frozen=True):
    manifest_path: str = Field(serialization_alias="manifestPath")
    artifact_ref: str = Field(serialization_alias="artifactRef")


class Live2DError(TaskError, frozen=True):
    stage: Stage | None = None


class Live2DState(BaseModel, frozen=True):
    task_id: TaskId = Field(serialization_alias="taskId")
    status: TaskStatus = TaskStatus.QUEUED
    stage: Stage | None = None
    result: Live2DResult | None = None
    error: Live2DError | None = None


@dataclass
class Live2DEntry:
    request: Live2DRequest
    state: Live2DState


class Live2DTasks:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.input_root = settings.live2d_input_root.resolve()
        self.output_root = settings.live2d_output_root.resolve()
        self.entries: dict[str, Live2DEntry] = {}
        self.lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live2d")

    def submit(self, request: Live2DRequest) -> Live2DState:
        task_id = request.task_id
        # 比较、登记、排队在同一锁内完成。重复请求取原任务快照。
        with self.lock:
            if entry := self.entries.get(task_id):
                if entry.request != request:
                    raise TaskConflict("该 taskId 已绑定其他 Live2D 参数")
                return entry.state
            resolve_source_file(self.input_root, request.source_image_ref)
            state = Live2DState(task_id=task_id)
            self.entries[task_id] = Live2DEntry(request, state)
            try:
                self.executor.submit(self.execute, request)
            except RuntimeError:
                del self.entries[task_id]
                raise
        logger.info("taskId=%s Live2D 任务已受理", task_id)
        return state

    def get(self, task_id: str) -> Live2DState | None:
        with self.lock:
            entry = self.entries.get(task_id)
            return entry.state if entry else None

    def close(self) -> None:
        self.executor.shutdown()

    def execute(self, request: Live2DRequest) -> None:
        task_id = request.task_id
        started_at = perf_counter()
        stage = Stage.PREPARING

        def save(
            status: TaskStatus,
            *,
            result: Live2DResult | None = None,
            error: Live2DError | None = None,
        ) -> None:
            with self.lock:
                self.entries[task_id].state = Live2DState(
                    task_id=task_id,
                    status=status,
                    stage=stage,
                    result=result,
                    error=error,
                )

        def report(next_stage: Stage) -> None:
            nonlocal stage
            stage = next_stage
            save(TaskStatus.RUNNING)

        try:
            report(stage)
            # 排队期间源文件可能被移除或替换，执行前重新解析。
            source = resolve_source_file(self.input_root, request.source_image_ref)
            final = self.output_root / f"{task_id}-{uuid4().hex}"
            with stage_task_directory(self.output_root, prefix=".live2d-task-") as temp:
                staged = Path(temp) / "artifact"
                manifest = generate_model(
                    source,
                    staged.parent,
                    staged,
                    self.settings,
                    task_id,
                    report,
                    model_name=request.model_name,
                )
                try:
                    publish_task_directory(self.output_root, staged, final)
                except (OSError, ValueError) as exc:
                    raise GenerationError(
                        "ARTIFACT_WRITE_FAILED", "模型包发布失败", Stage.EXPORTING
                    ) from exc
            artifact_ref = f"{final.name}/model.zip"
            save(
                TaskStatus.SUCCEEDED,
                result=Live2DResult(manifest_path=manifest, artifact_ref=artifact_ref),
            )
            logger.info(
                "taskId=%s 模型包已发布 manifestPath=%s artifactRef=%s",
                task_id,
                manifest,
                artifact_ref,
            )
        except Exception as exc:
            logger.exception("taskId=%s stage=%s Live2D 失败", task_id, stage)
            code, message = "LIVE2D_TASK_FAILED", "Live2D 任务处理失败"
            if isinstance(exc, GenerationError):
                code, message = exc.code, str(exc)
            elif isinstance(exc, FileNotFoundError) and stage == Stage.PREPARING:
                code, message = "SOURCE_IMAGE_NOT_FOUND", "共享输入目录中找不到源图片"
            save(
                TaskStatus.FAILED,
                error=Live2DError(code=code, message=message, stage=stage),
            )
        finally:
            logger.info(
                "taskId=%s Live2D 任务结束，总耗时 %.2f 秒",
                task_id,
                perf_counter() - started_at,
            )
