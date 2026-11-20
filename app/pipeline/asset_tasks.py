import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from app.audio.extract import AudioStreamNotFound, extract_audio
from app.core.config import Settings
from app.dialogue.transcribe import DialogueModelUnavailable, DialogueTranscriber
from app.pipeline.asset_task_models import (
    AssetKind,
    AssetTaskRequest,
    AssetTaskState,
    AssetTaskStatus,
    AudioResult,
    AudioStage,
    BranchResult,
    BranchStage,
    BranchState,
    DialogueStage,
    ShotStage,
)
from app.pipeline.character_task_models import (
    TaskError,
    TaskProgress,
    TaskStage,
    TaskStatus,
)
from app.pipeline.character_tasks import RunTask, TaskConflict, task_error
from app.pipeline.dialogue_extraction import extract_dialogue
from app.pipeline.shot_extraction import extract_shots
from app.storage.shared_files import (
    InvalidSourceReference,
    SourceVideoNotFound,
    checked_file,
    publish_task_directory,
    resolve_source_video,
    stage_task_directory,
)
from app.video.ffmpeg import VideoError, VideoTimeout

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
        self.shot_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="shot-extraction"
        )
        self.dialogue_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dialogue-extraction"
        )
        self.transcribe_dialogue = DialogueTranscriber(settings).transcribe
        self.executors = {
            AssetKind.CHARACTERS: character_executor,
            AssetKind.AUDIO: self.audio_executor,
            AssetKind.SHOTS: self.shot_executor,
            AssetKind.DIALOGUE: self.dialogue_executor,
        }
        self.entries: dict[str, AssetTaskEntry] = {}
        self.lock = Lock()

    def submit(self, request: AssetTaskRequest) -> AssetTaskState:
        # 只有人物分支使用抽帧间隔。有该分支时先归一化缺省值再比较幂等参数。
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
            audio_future: Future[BranchState] | None = None
            # 音频和对白用不同线程。联合任务通过已发布的 WAV 共享一次源音轨解码。
            for kind in entry.branches:
                try:
                    future = self.executors[kind].submit(
                        self.execute_branch,
                        effective,
                        kind,
                        audio_future if kind is AssetKind.DIALOGUE else None,
                    )
                    if kind is AssetKind.AUDIO:
                        audio_future = future
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
        self.shot_executor.shutdown()
        self.dialogue_executor.shutdown()

    def execute_branch(
        self,
        task: AssetTaskRequest,
        kind: AssetKind,
        audio_future: Future[BranchState] | None,
    ) -> BranchState:
        task_id = task.task_id
        started_at = stage_started_at = perf_counter()
        state = BranchState(status=TaskStatus.RUNNING)

        def save(updated: BranchState) -> None:
            nonlocal state
            with self.lock:
                state = updated
                self.entries[task_id].branches[kind] = state

        def report(
            stage: BranchStage,
            progress: TaskProgress | None = None,
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
            result: BranchResult
            if kind is AssetKind.CHARACTERS:
                result = self.run_characters(task, source, report)
            elif kind is AssetKind.SHOTS:
                result = extract_shots(
                    task_id, source, self.output_root, self.settings, report
                )
            elif kind is AssetKind.DIALOGUE:
                result = extract_dialogue(
                    task_id,
                    source,
                    self.output_root,
                    self.settings,
                    self.transcribe_dialogue,
                    report,
                    (lambda: self.shared_audio(audio_future))
                    if AssetKind.AUDIO in task.outputs
                    else None,
                )
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
        return state

    def shared_audio(self, future: Future[BranchState] | None) -> Path:
        if future is None:
            raise VideoError("共享音频分支无法排队")
        branch = future.result()
        if isinstance(branch.result, AudioResult):
            return checked_file(
                self.output_root, self.output_root, branch.result.audio_ref
            )
        code = branch.error.code if branch.error else None
        if code == "AUDIO_STREAM_NOT_FOUND":
            raise AudioStreamNotFound("源视频没有音轨")
        if code == "AUDIO_EXTRACTION_TIMEOUT":
            raise VideoTimeout("共享音轨处理超时")
        raise VideoError("共享音频分支未生成有效 WAV")


def branch_error(
    exc: Exception, kind: AssetKind, stage: BranchStage | None
) -> TaskError:
    if kind is AssetKind.CHARACTERS or isinstance(
        exc, (SourceVideoNotFound, InvalidSourceReference)
    ):
        return task_error(exc, stage if isinstance(stage, TaskStage) else None)
    if kind is AssetKind.SHOTS:
        if isinstance(exc, VideoTimeout):
            return TaskError(code="SHOT_EXTRACTION_TIMEOUT", message="分镜提取超时")
        if stage is ShotStage.EXTRACTING_PREVIEWS:
            return TaskError(code="SHOT_PREVIEW_FAILED", message="分镜代表图提取失败")
        if stage is ShotStage.WRITING_RESULTS:
            return TaskError(code="SHOT_WRITE_FAILED", message="分镜清单校验或发布失败")
        return TaskError(code="SHOT_DETECTION_FAILED", message="无法识别视频镜头")
    if kind is AssetKind.DIALOGUE:
        if isinstance(exc, AudioStreamNotFound):
            return TaskError(code="AUDIO_STREAM_NOT_FOUND", message="源视频没有音轨")
        if isinstance(exc, (VideoTimeout, TimeoutError)):
            return TaskError(code="DIALOGUE_EXTRACTION_TIMEOUT", message="对白提取超时")
        if isinstance(exc, DialogueModelUnavailable):
            return TaskError(
                code="DIALOGUE_MODEL_UNAVAILABLE", message="语音识别模型无法加载"
            )
        if stage is DialogueStage.WRITING_RESULTS:
            return TaskError(
                code="DIALOGUE_WRITE_FAILED", message="对白清单校验或发布失败"
            )
        if stage is DialogueStage.TRANSCRIBING_DIALOGUE:
            return TaskError(
                code="DIALOGUE_TRANSCRIPTION_FAILED", message="语音转写失败"
            )
        return TaskError(code="DIALOGUE_AUDIO_PREP_FAILED", message="对白音轨准备失败")
    if isinstance(exc, AudioStreamNotFound):
        return TaskError(code="AUDIO_STREAM_NOT_FOUND", message="源视频没有音轨")
    if isinstance(exc, VideoTimeout):
        return TaskError(code="AUDIO_EXTRACTION_TIMEOUT", message="音轨提取超时")
    if stage is AudioStage.WRITING_RESULTS:
        return TaskError(code="AUDIO_WRITE_FAILED", message="音轨结果校验或发布失败")
    return TaskError(code="AUDIO_EXTRACTION_FAILED", message="音轨提取失败")
