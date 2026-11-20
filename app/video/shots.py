import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.video.ffmpeg import VideoError
from app.video.selected_frames import (
    VideoFrame,
    extract_selected_jpegs,
    frames_near_targets,
    playback_frames,
    scan_frame_timeline,
    video_duration_ms,
)

CUT_FRAME = re.compile(
    r"^\[Parsed_metadata_\d+ @ [^\]]+\] frame:\s*(\d+)\s+pts:", re.MULTILINE
)
FRAME_APPEARANCE = re.compile(
    r"^\[Parsed_showinfo_\d+ @ [^\]]+\] n:\s*(\d+)\s+pts:.*?"
    r"mean:\[(\d+) (\d+) (\d+)\].*?stdev:\[([\d.]+)",
    re.MULTILINE,
)


class Shot(BaseModel):
    model_config = ConfigDict(validate_by_name=True, strict=True)

    shot_index: int = Field(alias="shotIndex", ge=1)
    start_ms: int = Field(alias="startMs", ge=0)
    end_ms: int = Field(alias="endMs", gt=0)
    preview_timestamp_ms: int = Field(alias="previewTimestampMs", ge=0)
    preview_path: str = Field(alias="previewPath", min_length=1)


class ShotManifest(BaseModel):
    model_config = ConfigDict(validate_by_name=True, strict=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    duration_ms: int = Field(alias="durationMs", gt=0)
    shots: list[Shot] = Field(min_length=1)


@dataclass(frozen=True)
class ShotAnalysis:
    manifest: ShotManifest
    frames: list[VideoFrame]
    previews: list[VideoFrame]


def cut_frame_indices(stderr: str, frame_count: int) -> set[int]:
    candidates = {int(index) for index in CUT_FRAME.findall(stderr)}
    statistics = FRAME_APPEARANCE.findall(stderr)
    if [int(index) for index, *_ in statistics] != list(range(frame_count)):
        raise VideoError("无法将切镜信号与连续的视频帧对应")
    appearance = [
        (int(luma), int(chroma_u), int(chroma_v), float(luma_deviation))
        for _, luma, chroma_u, chroma_v, luma_deviation in statistics
    ]
    flashes = set()
    for index in candidates:
        if not 0 < index < frame_count - 1:
            continue
        luma, chroma_u, chroma_v, deviation = appearance[index]
        if (
            luma < 225
            or abs(chroma_u - 128) > 8
            or abs(chroma_v - 128) > 8
            or deviation > 5
        ):
            continue
        previous, following = appearance[index - 1], appearance[index + 1]
        # 两侧画面统计接近且中间仅有一张近白帧，把成对信号视为闪白。
        if all(
            abs(before - after) <= 8
            for before, after in zip(previous, following, strict=True)
        ):
            flashes.update((index, index + 1))
    return candidates - flashes


def detect_shots(source: Path, settings: Settings) -> ShotAnalysis:
    duration_ms = video_duration_ms(source, settings)
    # 检测时排除常见的片底字幕区，后续代表图仍从完整画面抽取。
    visible_height = 1 - settings.shot_ignore_bottom_fraction
    filters = (
        f"scale=320:-2,format=yuv420p,crop=iw:'max(2,trunc(ih*{visible_height:g}/2)*2)':0:0,"
        f"scdet=threshold={settings.shot_scene_threshold:g},"
        "metadata=mode=print:key=lavfi.scd.time,showinfo"
    )
    timeline, stderr = scan_frame_timeline(source, settings, filters)
    candidates = cut_frame_indices(stderr, len(timeline))
    # 按向外发布的毫秒精度去重，原始帧号和 PTS 仍用于回读校验。
    frames = playback_frames(timeline, duration_ms)
    cuts = sorted(
        {
            frame.timestamp_ms
            for frame in frames
            if frame.index in candidates and frame.timestamp_ms > 0
        }
    )
    boundaries = [0, *cuts, duration_ms]
    times = [frame.timestamp_ms for frame in frames]
    selected = []
    shots = []
    for shot_index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), start=1):
        frame = frames_near_targets(frames, times, start, end, (0.5,))[0]
        selected.append(frame)
        shots.append(
            Shot(
                shot_index=shot_index,
                start_ms=start,
                end_ms=end,
                preview_timestamp_ms=frame.timestamp_ms,
                preview_path=f"previews/shot-{shot_index:06d}.jpg",
            )
        )
    return ShotAnalysis(
        ShotManifest(schema_version=1, duration_ms=duration_ms, shots=shots),
        frames,
        selected,
    )


def extract_shot_previews(
    source: Path, directory: Path, selected: list[VideoFrame], settings: Settings
) -> None:
    extract_selected_jpegs(
        source, directory / "previews", selected, settings, prefix="shot", max_width=640
    )
