from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.backgrounds.select import (
    DetectionStatus,
    DetectPeople,
    ReviewStatus,
    review_status,
    score_candidate,
)
from app.core.config import Settings
from app.pipeline.asset_task_models import BackgroundResult, BackgroundStage
from app.storage.shared_files import (
    checked_file,
    publish_task_directory,
    stage_task_directory,
)
from app.video.ffmpeg import VideoError, VideoTimeout, validate_jpeg
from app.video.selected_frames import (
    VideoFrame,
    extract_selected_jpegs,
    frames_near_targets,
    playback_frames,
    scan_frame_timeline,
    video_duration_ms,
)
from app.video.shots import ShotManifest, detect_shots


class BackgroundShotManifestInvalid(ValueError):
    """无法使用指定的分镜清单。"""


class BackgroundShotsFailed(RuntimeError):
    """本次分镜依赖未成功。"""


class BackgroundImage(BaseModel):
    shot_index: int = Field(serialization_alias="shotIndex")
    start_ms: int = Field(serialization_alias="startMs")
    end_ms: int = Field(serialization_alias="endMs")
    timestamp_ms: int = Field(serialization_alias="timestampMs")
    image_path: str = Field(serialization_alias="imagePath")
    method: Literal["SOURCE_FRAME"] = "SOURCE_FRAME"
    person_status: DetectionStatus = Field(serialization_alias="personStatus")
    subtitle_status: DetectionStatus = Field(serialization_alias="subtitleStatus")
    review_status: ReviewStatus = Field(serialization_alias="reviewStatus")


class BackgroundManifest(BaseModel):
    schema_version: Literal[1] = Field(1, serialization_alias="schemaVersion")
    duration_ms: int = Field(serialization_alias="durationMs")
    backgrounds: list[BackgroundImage]


def read_shot_manifest(root: Path, reference: str, duration_ms: int) -> ShotManifest:
    try:
        path = checked_file(root, root, reference)
        manifest = ShotManifest.model_validate_json(path.read_bytes())
        if manifest.duration_ms != duration_ms:
            raise ValueError("分镜清单与源视频时长不一致")
        start = 0
        for index, shot in enumerate(manifest.shots, start=1):
            if (
                shot.shot_index != index
                or shot.start_ms != start
                or not start <= shot.preview_timestamp_ms < shot.end_ms <= duration_ms
            ):
                raise ValueError("分镜编号、镜头区间或代表帧时间不符合契约")
            validate_jpeg(checked_file(path.parent, path.parent, shot.preview_path))
            start = shot.end_ms
        if start != duration_ms:
            raise ValueError("镜头区间没有覆盖全片")
    except (OSError, RuntimeError, ValueError, VideoError) as exc:
        raise BackgroundShotManifestInvalid(f"分镜引用 {reference} 无效") from exc
    return manifest


def select_backgrounds(
    manifest: ShotManifest,
    frames: list[VideoFrame],
    source: Path,
    temporary: Path,
    staged: Path,
    settings: Settings,
    detect_people: DetectPeople,
) -> list[BackgroundImage]:
    timestamps = [frame.timestamp_ms for frame in frames]
    groups = [
        frames_near_targets(
            frames, timestamps, shot.start_ms, shot.end_ms, (0.25, 0.5, 0.75)
        )
        for shot in manifest.shots
    ]
    ordered = sorted(
        (frame for group in groups for frame in group), key=lambda frame: frame.index
    )
    images = extract_selected_jpegs(source, temporary / "candidates", ordered, settings)
    by_frame = dict(zip(ordered, images, strict=True))
    (staged / "images").mkdir()
    backgrounds = []
    # 当前工程暂无画面字幕检测。保留 UNKNOWN，避免把未经验证的原帧标成干净背景。
    subtitles = DetectionStatus.UNKNOWN
    for shot, group in zip(manifest.shots, groups, strict=True):
        center = (shot.start_ms + shot.end_ms) / 2
        scores = {
            frame: score_candidate(by_frame[frame], detect_people) for frame in group
        }
        # 优先选人物遮挡少的画面，再比较清晰度和距镜头中点的距离。
        frame = min(
            group,
            key=lambda candidate: (
                scores[candidate][0] is DetectionStatus.DETECTED,
                scores[candidate][1],
                -scores[candidate][2],
                abs(candidate.actual_ms - center),
            ),
        )
        people = scores[frame][0]
        image_path = f"images/shot-{shot.shot_index:06d}.jpg"
        by_frame[frame].rename(staged / image_path)
        backgrounds.append(
            BackgroundImage(
                shot_index=shot.shot_index,
                start_ms=shot.start_ms,
                end_ms=shot.end_ms,
                timestamp_ms=frame.timestamp_ms,
                image_path=image_path,
                person_status=people,
                subtitle_status=subtitles,
                review_status=review_status(people, subtitles),
            )
        )
    return backgrounds


def extract_backgrounds(
    task_id: str,
    source: Path,
    output_root: Path,
    settings: Settings,
    detect_people: DetectPeople,
    report: Callable[[BackgroundStage], None],
    resolve_shot_ref: Callable[[], str] | None = None,
) -> BackgroundResult:
    report(BackgroundStage.RESOLVING_SHOTS)
    frames = None
    if resolve_shot_ref is None:
        try:
            analysis = detect_shots(source, settings)
        except VideoTimeout:
            raise
        except (OSError, RuntimeError, ValueError, VideoError) as exc:
            raise BackgroundShotsFailed("源视频分镜失败") from exc
        manifest, frames = analysis.manifest, analysis.frames
    else:
        reference = resolve_shot_ref()
        manifest = read_shot_manifest(
            output_root, reference, video_duration_ms(source, settings)
        )

    report(BackgroundStage.SELECTING_BACKGROUND_FRAMES)
    if frames is None:
        timeline, _ = scan_frame_timeline(source, settings)
        frames = playback_frames(timeline, manifest.duration_ms)
    with stage_task_directory(output_root) as temporary:
        workspace = Path(temporary)
        staged = workspace / "result"
        staged.mkdir()
        backgrounds = select_backgrounds(
            manifest, frames, source, workspace, staged, settings, detect_people
        )
        report(BackgroundStage.WRITING_RESULTS)
        for image in backgrounds:
            checked_file(staged, staged, image.image_path)
        payload = BackgroundManifest(
            duration_ms=manifest.duration_ms, backgrounds=backgrounds
        )
        (staged / "backgrounds.json").write_text(
            payload.model_dump_json(indent=2, by_alias=True) + "\n",
            encoding="utf-8",
        )
        destination = output_root / f"{task_id}-background-{uuid4().hex}"
        publish_task_directory(output_root, staged, destination)
    return BackgroundResult(
        manifest_ref=(destination / "backgrounds.json")
        .relative_to(output_root)
        .as_posix(),
        background_count=len(backgrounds),
    )
