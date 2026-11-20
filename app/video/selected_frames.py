from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal
from math import ceil, floor
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.config import Settings
from app.video.ffmpeg import (
    VideoError,
    frame_timestamps,
    probe_video,
    run_tool,
    validate_jpeg,
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


@dataclass(frozen=True)
class VideoFrame:
    index: int
    actual_ms: float

    @property
    def timestamp_ms(self) -> int:
        return floor(self.actual_ms + 0.5)


def video_duration_ms(source: Path, settings: Settings) -> int:
    return ceil(Decimal(str(probe_video(source, settings).duration_seconds)) * 1000)


def scan_frame_timeline(
    source: Path, settings: Settings, filters: str = "showinfo=checksum=0"
) -> tuple[list[VideoFrame], str]:
    result = run_tool(
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
    timestamps = frame_timestamps(result.stderr)
    if not timestamps or any(
        first > second for first, second in zip(timestamps, timestamps[1:])
    ):
        raise VideoError("无法读取有效的视频帧时间轴")
    return [
        VideoFrame(index, timestamp) for index, timestamp in enumerate(timestamps)
    ], result.stderr


def playback_frames(frames: list[VideoFrame], duration_ms: int) -> list[VideoFrame]:
    return [
        frame
        for frame in frames
        if frame.actual_ms >= 0 and frame.timestamp_ms < duration_ms
    ]


def frames_near_targets(
    frames: list[VideoFrame],
    timestamps: list[int],
    start_ms: int,
    end_ms: int,
    fractions: tuple[float, ...],
) -> list[VideoFrame]:
    left = bisect_left(timestamps, start_ms)
    right = bisect_left(timestamps, end_ms, lo=left)
    if left == right:
        raise VideoError(f"镜头区间 {start_ms}–{end_ms} 毫秒内没有可用原帧")
    selected = []
    for fraction in fractions:
        target = start_ms + (end_ms - start_ms) * fraction
        after = bisect_left(timestamps, target, lo=left, hi=right)
        before = max(left, after - 1)
        nearest = min(
            before,
            min(right - 1, after),
            key=lambda index: abs(timestamps[index] - target),
        )
        selected.append(frames[nearest])
    return list(dict.fromkeys(selected))


def extract_selected_jpegs(
    source: Path,
    directory: Path,
    selected: list[VideoFrame],
    settings: Settings,
    *,
    prefix: str = "frame",
    max_width: int | None = None,
) -> list[Path]:
    directory.mkdir()
    # 按原帧号批量提取 JPEG，回读实际 PTS，避免 seek 误取相邻镜头。
    expression = "+".join(f"eq(n,{frame.index})" for frame in selected)
    scale = f",scale='min({max_width},iw)':-2" if max_width is not None else ""
    with TemporaryDirectory(prefix=".frame-filter-", dir=directory.parent) as temporary:
        filter_path = Path(temporary).resolve() / "select.filter"
        filter_path.write_text(
            f"select='{expression}'{scale},showinfo=checksum=0",
            encoding="ascii",
        )
        result = run_tool(
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
                f"{prefix}-%06d.jpg",
            ],
            timeout=300,
            cwd=directory,
        )
    if frame_timestamps(result.stderr) != [frame.actual_ms for frame in selected]:
        raise VideoError("截图的实际时间与所选原帧不一致")
    if len(list(directory.glob(f"{prefix}-*.jpg"))) != len(selected):
        raise VideoError("截图数量与所选原帧不一致")
    files = [
        directory / f"{prefix}-{index:06d}.jpg" for index in range(1, len(selected) + 1)
    ]
    for file in files:
        validate_jpeg(file)
    return files
