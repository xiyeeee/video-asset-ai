import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import perf_counter

from app.core.config import Settings
from app.pipeline.character_extraction import CharacterModelsUnavailable, ReportStage
from app.pipeline.character_task_models import (
    TaskError,
    TaskProgress,
    TaskRequest,
    TaskResult,
    TaskStage,
    TaskState,
    TaskStatus,
)
from app.storage.shared_files import (
    InvalidSourceReference,
    SourceVideoNotFound,
    resolve_source_video,
)
from app.video.ffmpeg import VideoTimeout

logger = logging.getLogger(__name__)
RunTask = Callable[[TaskRequest, Path, ReportStage], TaskResult]

FAILURES_BY_STAGE = {
    TaskStage.EXTRACTING_FRAMES: (
        "FRAME_EXTRACTION_FAILED",
        "视频抽帧失败，请检查源视频",
    ),
    TaskStage.DETECTING_CHARACTERS: ("CHARACTER_DETECTION_FAILED", "人物检测失败"),
    TaskStage.SEGMENTING_CHARACTERS: (
        "CHARACTER_SEGMENTATION_FAILED",
        "人物分割或素材生成失败",
    ),
    TaskStage.WRITING_RESULTS: ("RESULT_WRITE_FAILED", "人物提取结果校验或发布失败"),
}


class TaskConflict(ValueError):
    """相同 taskId 已绑定其他有效参数。"""


@dataclass
class TaskEntry:
    request: TaskRequest
    state: TaskState


class CharacterTasks:
    def __init__(self, settings: Settings, run_task: RunTask) -> None:
        self.input_root = settings.character_input_root.resolve()
        self.default_interval = settings.character_frame_interval_seconds
        self.run_task = run_task
        self.entries: dict[str, TaskEntry] = {}
        self.lock = Lock()
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="character-extraction"
        )

    def submit(self, request: TaskRequest) -> TaskState:
        task_id = request.task_id
        interval = request.frame_interval_seconds or self.default_interval
        effective = request.model_copy(update={"frame_interval_seconds": interval})

        # 参数比较、任务登记和排队在同一把锁内。重复请求只能取快照，不能重复排队。
        with self.lock:
            existing = self.entries.get(task_id)
            if existing is not None:
                if existing.request != effective:
                    raise TaskConflict("该 taskId 已绑定其他人物提取参数")
                logger.info(
                    "taskId=%s 重复提交，返回 status=%s", task_id, existing.state.status
                )
                return existing.state
            resolve_source_video(self.input_root, effective.source_video_ref)
            state = TaskState(task_id=task_id)
            self.entries[task_id] = TaskEntry(request=effective, state=state)
            try:
                self.executor.submit(self.execute, effective)
            except RuntimeError:
                del self.entries[task_id]
                raise
            logger.info("taskId=%s 已受理 frameIntervalSeconds=%g", task_id, interval)
            return state

    def get(self, task_id: str) -> TaskState | None:
        with self.lock:
            entry = self.entries.get(task_id)
            return entry.state if entry else None

    def close(self) -> None:
        self.executor.shutdown()

    def execute(self, task: TaskRequest) -> None:
        task_id = task.task_id
        started_at = stage_started_at = perf_counter()
        state = TaskState(task_id=task_id, status=TaskStatus.RUNNING)

        def save_state(updated: TaskState) -> None:
            nonlocal state
            state = updated
            with self.lock:
                self.entries[task_id].state = state

        def complete_stage(now: float) -> None:
            if state.stage is not None:
                logger.info(
                    "taskId=%s stage=%s 完成，耗时 %.2f 秒",
                    task_id,
                    state.stage,
                    now - stage_started_at,
                )

        def report(stage: TaskStage, progress: TaskProgress) -> None:
            nonlocal stage_started_at
            now = perf_counter()
            complete_stage(now)
            stage_started_at = now
            save_state(state.model_copy(update={"stage": stage, "progress": progress}))
            logger.info(
                "taskId=%s stage=%s 开始 frameCount=%s detectedCharacterCount=%s",
                task_id,
                stage,
                progress.frame_count,
                progress.detected_character_count,
            )

        save_state(state)
        try:
            source = resolve_source_video(self.input_root, task.source_video_ref)
            result = self.run_task(task, source, report)
        except Exception as exc:
            error = task_error(exc, state.stage)
            logger.exception(
                "taskId=%s stage=%s status=FAILED code=%s 耗时 %.2f 秒",
                task_id,
                state.stage,
                error.code,
                perf_counter() - started_at,
            )
            save_state(
                state.model_copy(update={"status": TaskStatus.FAILED, "error": error})
            )
        else:
            now = perf_counter()
            complete_stage(now)
            save_state(
                state.model_copy(
                    update={"status": TaskStatus.SUCCEEDED, "result": result}
                )
            )
            logger.info(
                "taskId=%s status=SUCCEEDED characterAssetCount=%d 耗时 %.2f 秒",
                task_id,
                result.character_asset_count,
                now - started_at,
            )


def task_error(exc: Exception, stage: TaskStage | None) -> TaskError:
    if isinstance(exc, SourceVideoNotFound):
        return TaskError(
            code="SOURCE_VIDEO_NOT_FOUND", message="共享输入目录中找不到源视频"
        )
    if isinstance(exc, InvalidSourceReference):
        return TaskError(code="SOURCE_VIDEO_REF_INVALID", message="源视频引用已失效")
    if isinstance(exc, CharacterModelsUnavailable):
        return TaskError(
            code="CHARACTER_MODELS_UNAVAILABLE", message="人物模型无法加载"
        )
    if isinstance(exc, VideoTimeout):
        return TaskError(code="FRAME_EXTRACTION_TIMEOUT", message="视频抽帧超时")
    code, message = FAILURES_BY_STAGE.get(
        stage, ("TASK_PROCESSING_FAILED", "人物提取任务处理失败")
    )
    return TaskError(code=code, message=message)
