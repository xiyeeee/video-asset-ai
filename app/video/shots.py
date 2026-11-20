import re
from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal
from math import ceil, floor
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.config import Settings
from app.video.ffmpeg import (
    VideoError,
    frame_timestamps,
    probe_video,
    run_tool,
    validate_jpeg,
)

CUT_FRAME = re.compile(
    r"^\[Parsed_metadata_\d+ @ [^\]]+\] frame:\s*(\d+)\s+pts:", re.MULTILINE
)
FRAME_APPEARANCE = re.compile(
    r"^\[Parsed_showinfo_\d+ @ [^\]]+\] n:\s*(\d+)\s+pts:.*?"
    r"mean:\[(\d+) (\d+) (\d+)\].*?stdev:\[([\d.]+)",
    re.MULTILINE,
)
INPUT_TIMELINE_OPTIONS = (
    "-hide_banner",
    "-nostats",
    "-loglevel",
    "info",
    "-xerror",
    "-copyts",
    "-start_at_zero",
)


class Shot(BaseModel):
    shot_index: int = Field(serialization_alias="shotIndex")
    start_ms: int = Field(serialization_alias="startMs")
    end_ms: int = Field(serialization_alias="endMs")
    preview_timestamp_ms: int = Field(serialization_alias="previewTimestampMs")
    preview_path: str = Field(serialization_alias="previewPath")


class ShotManifest(BaseModel):
    schema_version: int = Field(1, serialization_alias="schemaVersion")
    duration_ms: int = Field(serialization_alias="durationMs")
    shots: list[Shot]


@dataclass(frozen=True)
class VideoFrame:
    index: int
    actual_ms: float

    @property
    def timestamp_ms(self) -> int:
        return floor(self.actual_ms + 0.5)


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


def detect_shots(
    source: Path, settings: Settings
) -> tuple[ShotManifest, list[VideoFrame]]:
    video = probe_video(source, settings)
    duration_ms = ceil(Decimal(str(video.duration_seconds)) * 1000)
    # 检测时排除常见的片底字幕区，后续代表图仍从完整画面抽取。
    visible_height = 1 - settings.shot_ignore_bottom_fraction
    filters = (
        f"scale=320:-2,format=yuv420p,crop=iw:'max(2,trunc(ih*{visible_height:g}/2)*2)':0:0,"
        f"scdet=threshold={settings.shot_scene_threshold:g},"
        "metadata=mode=print:key=lavfi.scd.time,showinfo"
    )
    scan = run_tool(
        settings.ffmpeg_path,
        [
            *INPUT_TIMELINE_OPTIONS,
            "-i",
            str(source),
            "-map",
            "0:V:0",
            "-vf",
            filters,
            "-fps_mode",
            "passthrough",
            "-f",
            "null",
            "-",
        ],
        timeout=300,
    )
    timestamps = frame_timestamps(scan.stderr)
    if not timestamps or any(
        first > second for first, second in zip(timestamps, timestamps[1:])
    ):
        raise VideoError("无法读取有效的视频帧时间轴")
    candidates = cut_frame_indices(scan.stderr, len(timestamps))
    # 按向外发布的毫秒精度去重。帧号仍来自全量扫描，保留原始 PTS 用于回读校验。
    frames = [
        VideoFrame(index, timestamp)
        for index, timestamp in enumerate(timestamps)
        if timestamp >= 0 and floor(timestamp + 0.5) < duration_ms
    ]
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
        left = bisect_left(times, start)
        right = bisect_left(times, end, lo=left)
        if left == right:
            raise VideoError(f"镜头 {shot_index} 内没有可用代表帧")
        middle = (start + end) / 2
        after = bisect_left(times, middle, lo=left, hi=right)
        before = max(left, after - 1)
        nearest = min(
            before, min(right - 1, after), key=lambda index: abs(times[index] - middle)
        )
        frame = frames[nearest]
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
    return ShotManifest(duration_ms=duration_ms, shots=shots), selected


def extract_shot_previews(
    source: Path, directory: Path, selected: list[VideoFrame], settings: Settings
) -> None:
    images = directory / "previews"
    images.mkdir()
    filter_path = directory.parent / "shot-select.filter"
    # 批量按原始帧号抽取，避免不同镜头逐一 seek 时误取到相邻镜头。
    expression = "+".join(f"eq(n,{frame.index})" for frame in selected)
    filter_path.write_text(
        f"select='{expression}',scale='min(640,iw)':-2,showinfo=checksum=0",
        encoding="ascii",
    )
    extract = run_tool(
        settings.ffmpeg_path,
        [
            *INPUT_TIMELINE_OPTIONS,
            "-i",
            str(source),
            "-map",
            "0:V:0",
            "-/filter:v",
            str(filter_path),
            "-fps_mode",
            "passthrough",
            "-c:v",
            "mjpeg",
            "-q:v",
            "2",
            "-start_number",
            "1",
            "-f",
            "image2",
            "shot-%06d.jpg",
        ],
        timeout=300,
        cwd=images,
    )
    if frame_timestamps(extract.stderr) != [frame.actual_ms for frame in selected]:
        raise VideoError("代表帧实际时间与镜头清单不一致")
    if len(list(images.glob("shot-*.jpg"))) != len(selected):
        raise VideoError("代表图数量与镜头清单不一致")
    for index in range(1, len(selected) + 1):
        validate_jpeg(images / f"shot-{index:06d}.jpg")
