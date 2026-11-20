import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from app.audio.extract import AudioStreamNotFound, extract_audio
from app.core.config import Settings
from app.pipeline.asset_task_models import (
    AssetKind,
    AssetTaskRequest,
    AssetTaskState,
    AssetTaskStatus,
    AudioResult,
    AudioStage,
    BranchState,
)
from app.pipeline.character_task_models import (
    TaskError,
    TaskProgress,
    TaskResult,
    TaskStage,
    TaskStatus,
)
from app.pipeline.character_tasks import RunTask, TaskConflict, task_error
from app.storage.shared_files import (
    InvalidSourceReference,
    SourceVideoNotFound,
    checked_file,
    publish_task_directory,
    resolve_source_video,
    stage_task_directory,
)
from app.video.ffmpeg import VideoTimeout

logger = logging.getLogger(__name__)


@dataclass
class AssetTaskEntry:
    request: AssetTaskRequest
    branches: dict[AssetKind, BranchState]

    def snapshot(self) -> AssetTaskState:
        states = {entry.status for entry in self.branches.values()}
        if states == {TaskStatus.QUEUED}:
            status = AssetTaskStatus.QUEUED
        elif states & {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            status = AssetTaskStatus.RUNNING
        elif states == {TaskStatus.SUCCEEDED}:
            status = AssetTaskStatus.SUCCEEDED
        elif TaskStatus.SUCCEEDED in states:
            status = AssetTaskStatus.PARTIALLY_SUCCEEDED
        else:
            status = AssetTaskStatus.FAILED
        return AssetTaskState(
            task_id=self.request.task_id, status=status, branches=self.branches.copy()
        )


class AssetTasks:
    def __init__(
        self,
        settings: Settings,
        run_characters: RunTask,
        character_executor: ThreadPoolExecutor,
    ) -> None:
        self.settings = settings
        self.input_root = settings.character_input_root.resolve()
        self.output_root = settings.character_output_root.resolve()
        self.run_characters = run_characters
        self.audio_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="audio-extraction"
        )
        self.executors = {
            AssetKind.CHARACTERS: character_executor,
            AssetKind.AUDIO: self.audio_executor,
        }
        self.entries: dict[str, AssetTaskEntry] = {}
        self.lock = Lock()

    def submit(self, request: AssetTaskRequest) -> AssetTaskState:
        # 音频分支不读抽帧间隔。人物分支把缺省间隔归一化后再比较幂等参数。
        interval = (
            request.frame_interval_seconds
            or self.settings.character_frame_interval_seconds
            if AssetKind.CHARACTERS in request.outputs
            else None
        )
        effective = request.model_copy(update={"frame_interval_seconds": interval})
        task_id = request.task_id
        with self.lock:
            existing = self.entries.get(task_id)
            if existing is not None:
                if existing.request != effective:
                    raise TaskConflict("该 taskId 已绑定其他素材提取参数")
                return existing.snapshot()
            resolve_source_video(self.input_root, request.source_video_ref)
            entry = AssetTaskEntry(
                effective, {kind: BranchState() for kind in sorted(request.outputs)}
            )
            self.entries[task_id] = entry
            logger.info(
                "taskId=%s 素材编排已受理 outputs=%s", task_id, list(entry.branches)
            )
            # 同一任务只登记一次，各分支进入各自的队列。人物分支继续使用原推理队列。
            for kind in entry.branches:
                try:
                    self.executors[kind].submit(self.execute_branch, effective, kind)
                except RuntimeError:
                    logger.exception("taskId=%s branch=%s 无法排队", task_id, kind)
                    entry.branches[kind] = BranchState(
                        status=TaskStatus.FAILED,
                        error=TaskError(
                            code="BRANCH_QUEUE_FAILED", message="素材分支无法排队"
                        ),
                    )
            return entry.snapshot()

    def get(self, task_id: str) -> AssetTaskState | None:
        with self.lock:
            entry = self.entries.get(task_id)
            return entry.snapshot() if entry else None

    def close(self) -> None:
        self.audio_executor.shutdown()

    def execute_branch(self, task: AssetTaskRequest, kind: AssetKind) -> None:
        task_id = task.task_id
        started_at = stage_started_at = perf_counter()
        state = BranchState(status=TaskStatus.RUNNING)

        def save(updated: BranchState) -> None:
            nonlocal state
            with self.lock:
                state = updated
                self.entries[task_id].branches[kind] = state

        def report(
            stage: TaskStage | AudioStage, progress: TaskProgress | None = None
        ) -> None:
            nonlocal stage_started_at
            now = perf_counter()
            if state.stage is not None:
                logger.info(
                    "taskId=%s branch=%s stage=%s 完成，耗时 %.2f 秒",
                    task_id,
                    kind,
                    state.stage,
                    now - stage_started_at,
                )
            stage_started_at = now
            save(state.model_copy(update={"stage": stage, "progress": progress}))
            logger.info("taskId=%s branch=%s stage=%s 开始", task_id, kind, stage)

        save(state)
        try:
            source = resolve_source_video(self.input_root, task.source_video_ref)
            result: TaskResult | AudioResult
            if kind is AssetKind.CHARACTERS:
                result = self.run_characters(task, source, report)
            else:
                report(AudioStage.EXTRACTING_AUDIO)
                with stage_task_directory(self.output_root) as temporary:
                    staged = Path(temporary) / "result"
                    staged.mkdir()
                    extract_audio(source, staged / "audio.wav", self.settings)
                    report(AudioStage.WRITING_RESULTS)
                    checked_file(staged, staged, "audio.wav")
                    destination = self.output_root / f"{task_id}-audio-{uuid4().hex}"
                    audio_ref = (
                        (destination / "audio.wav")
                        .relative_to(self.output_root)
                        .as_posix()
                    )
                    publish_task_directory(self.output_root, staged, destination)
                    result = AudioResult(audio_ref=audio_ref)
        except Exception as exc:
            error = branch_error(exc, kind, state.stage)
            logger.exception(
                "taskId=%s branch=%s stage=%s code=%s 失败，耗时 %.2f 秒",
                task_id,
                kind,
                state.stage,
                error.code,
                perf_counter() - started_at,
            )
            save(state.model_copy(update={"status": TaskStatus.FAILED, "error": error}))
        else:
            logger.info(
                "taskId=%s branch=%s stage=%s 完成，阶段耗时 %.2f 秒，总耗时 %.2f 秒",
                task_id,
                kind,
                state.stage,
                perf_counter() - stage_started_at,
                perf_counter() - started_at,
            )
            save(
                state.model_copy(
                    update={"status": TaskStatus.SUCCEEDED, "result": result}
                )
            )


def branch_error(
    exc: Exception, kind: AssetKind, stage: TaskStage | AudioStage | None
) -> TaskError:
    if kind is AssetKind.CHARACTERS or isinstance(
        exc, (SourceVideoNotFound, InvalidSourceReference)
    ):
        return task_error(exc, stage if isinstance(stage, TaskStage) else None)
    if isinstance(exc, AudioStreamNotFound):
        return TaskError(code="AUDIO_STREAM_NOT_FOUND", message="源视频没有音轨")
    if isinstance(exc, VideoTimeout):
        return TaskError(code="AUDIO_EXTRACTION_TIMEOUT", message="音轨提取超时")
    if stage is AudioStage.WRITING_RESULTS:
        return TaskError(code="AUDIO_WRITE_FAILED", message="音轨结果校验或发布失败")
    return TaskError(code="AUDIO_EXTRACTION_FAILED", message="音轨提取失败")
