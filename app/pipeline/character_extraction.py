from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.config import Settings
from app.pipeline.character_task_models import (
    TaskProgress,
    TaskRequest,
    TaskResult,
    TaskStage,
)
from app.storage.shared_files import (
    checked_file,
    publish_task_directory,
    stage_task_directory,
)
from app.video.ffmpeg import extract_frames

if TYPE_CHECKING:
    from PIL.Image import Image

    from app.characters.extract import Detection, PersonModels

ReportStage = Callable[[TaskStage, TaskProgress], None]


class CharacterModelsUnavailable(RuntimeError):
    """人物模型无法加载。"""


class CharacterExtraction:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.output_root = settings.character_output_root.resolve()

    @cached_property
    def person_models(self) -> "PersonModels":
        try:
            from app.characters.models import GroundedSam2

            return GroundedSam2(self.settings, 0.25)
        except Exception as exc:
            raise CharacterModelsUnavailable(
                "无法加载 Grounding DINO 或 SAM 2"
            ) from exc

    def detect_people(self, image: "Image") -> list["Detection"]:
        return self.person_models.detect(image)

    def run(self, task: TaskRequest, source: Path, report: ReportStage) -> TaskResult:
        interval = (
            task.frame_interval_seconds
            or self.settings.character_frame_interval_seconds
        )
        report(TaskStage.EXTRACTING_FRAMES, TaskProgress())
        with stage_task_directory(self.output_root) as temporary:
            staged = Path(temporary) / "result"
            frames_dir = staged / "frames"
            characters_dir = staged / "characters"
            frames_manifest = frames_dir / "frames.json"

            frames = extract_frames(source, frames_dir, interval, self.settings)
            progress = TaskProgress(frame_count=len(frames))
            report(TaskStage.DETECTING_CHARACTERS, progress)
            models = self.person_models
            from app.characters.extract import extract_characters, select_frames

            def report_detected(count: int) -> None:
                nonlocal progress
                progress = TaskProgress(
                    frame_count=len(frames), detected_character_count=count
                )
                report(TaskStage.SEGMENTING_CHARACTERS, progress)

            manifest = extract_characters(
                frames_manifest,
                select_frames(frames_manifest, [frame["path"] for frame in frames]),
                characters_dir,
                models,
                on_detected=report_detected,
                on_writing=lambda: report(TaskStage.WRITING_RESULTS, progress),
                whole_milliseconds=True,
            )
            checked_file(staged, characters_dir, "characters.json")
            source_manifest = checked_file(
                staged, characters_dir, manifest.frames_manifest
            )
            for frame in manifest.frames:
                checked_file(staged, source_manifest.parent, frame.source_frame)
                checked_file(staged, characters_dir, frame.debug_image)
            for character in manifest.characters:
                checked_file(staged, characters_dir, character.png_path)
            # 每次执行使用新目录。重启后重复提交时可以保留 Java 已持有的历史引用。
            destination = self.output_root / f"{task.task_id}-{uuid4().hex}"
            manifest_ref = (destination / "characters" / "characters.json").relative_to(
                self.output_root
            )
            publish_task_directory(self.output_root, staged, destination)
            return TaskResult(
                manifest_ref=manifest_ref.as_posix(),
                character_asset_count=len(manifest.characters),
            )
