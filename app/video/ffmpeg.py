import json
import logging
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import TypedDict

from app.core.config import Settings

logger = logging.getLogger(__name__)
SHOWINFO = re.compile(r"^\[Parsed_showinfo_\d+ @ [^\]]+\] (.*)$", re.MULTILINE)
TIME_BASE = re.compile(r"config in time_base: (\d+/\d+),")
FRAME_PTS = re.compile(r"n:\s*\d+\s+pts:\s*(-?\d+)\s")


class VideoError(Exception):
    """视频工具调用或结果校验失败。"""


@dataclass(frozen=True)
class VideoInfo:
    duration_seconds: float
    width: int
    height: int


class Frame(TypedDict):
    path: str
    timestampMs: float


def run_tool(
    executable: str,
    args: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    path = shutil.which(executable)
    if path is None:
        raise VideoError(f"找不到可执行工具 {executable}，请检查 PATH 或 .env 配置")
    try:
        return subprocess.run(
            [str(Path(path).absolute()), *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"{executable} 运行超过 {timeout} 秒") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-2400:]
        raise VideoError(f"{executable} 退出码 {exc.returncode}\n{detail}") from exc
    except OSError as exc:
        raise VideoError(f"无法运行 {executable}，{exc}") from exc


def tool_available(executable: str) -> bool:
    try:
        run_tool(executable, ["-version"], timeout=2)
    except VideoError:
        return False
    return True


def probe_video(source: Path, settings: Settings) -> VideoInfo:
    logger.info("开始探测视频 %s", source)
    result = run_tool(
        settings.ffprobe_path,
        [
            "-v",
            "error",
            "-select_streams",
            "V:0",
            "-show_entries",
            "stream=width,height,duration:format=duration",
            "-of",
            "json",
            str(source),
        ],
        timeout=30,
    )
    data = json.loads(result.stdout)
    if not data.get("streams"):
        raise VideoError("输入文件没有视频流")
    stream = data["streams"][0]
    for duration in (data.get("format", {}).get("duration"), stream.get("duration")):
        if duration in (None, "N/A"):
            continue
        duration_seconds = float(duration)
        if math.isfinite(duration_seconds) and duration_seconds > 0:
            break
    else:
        raise VideoError("无法读取有效的视频时长")

    width, height = stream.get("width", 0), stream.get("height", 0)
    if width <= 0 or height <= 0:
        raise VideoError("无法读取有效的视频分辨率")
    logger.info("视频探测完成，%.3f 秒，%d×%d", duration_seconds, width, height)
    return VideoInfo(duration_seconds, width, height)


def frame_timestamps(stderr: str) -> list[float]:
    time_base = None
    timestamps = []
    for line in SHOWINFO.findall(stderr):
        if match := TIME_BASE.match(line):
            time_base = Fraction(match[1])
        elif (match := FRAME_PTS.match(line)) and time_base is not None:
            # 用整数 PTS 和时间基计算毫秒，避免 pts_time 日志的显示精度损失。
            timestamps.append(float(int(match[1]) * time_base * 1000))
    return timestamps


def validate_frames(directory: Path, timestamps: list[float]) -> list[Frame]:
    images = sorted(directory.glob("frame-*.jpg"))
    if not timestamps or len(images) != len(timestamps):
        raise VideoError(
            f"截图校验失败，得到 {len(images)} 张 JPEG 和 {len(timestamps)} 个时间戳"
        )

    frames: list[Frame] = []
    previous_timestamp = -1.0
    for index, (image, timestamp) in enumerate(
        zip(images, timestamps, strict=True), start=1
    ):
        if timestamp < 0 or timestamp <= previous_timestamp:
            raise VideoError(f"第 {index} 张截图的时间戳无效")
        if image.name != f"frame-{index:06d}.jpg" or image.stat().st_size < 4:
            raise VideoError(f"截图文件无效 {image.name}")
        with image.open("rb") as file:
            header = file.read(2)
            file.seek(-2, 2)
            if header != b"\xff\xd8" or file.read(2) != b"\xff\xd9":
                raise VideoError(f"JPEG 截图不完整 {image.name}")
        frames.append({"path": image.name, "timestampMs": timestamp})
        previous_timestamp = timestamp
    return frames


def check_output_directory(output: Path) -> None:
    if output.is_symlink() or (
        output.exists() and (not output.is_dir() or any(output.iterdir()))
    ):
        raise VideoError(
            f"输出路径已存在且无法写入新结果 {output}，请指定空目录或新目录"
        )


def extract_frames(
    source: Path, output: Path, interval_seconds: float, settings: Settings
) -> list[Frame]:
    started_at = perf_counter()
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise VideoError("抽帧间隔必须是有限正数")
    source = source.resolve()
    if not source.is_file():
        raise VideoError(f"输入文件不存在或不是普通文件 {source}")
    output = output.absolute()
    check_output_directory(output)
    video = probe_video(source, settings)
    logger.info("开始抽帧，间隔 %g 秒，输出目录 %s", interval_seconds, output)

    # 用容器播放起点归零，每个时间区间保留第一帧。微小容差处理浮点边界。
    interval = f"max({interval_seconds},TB)"
    window = f"floor((t+TB*1e-6)/{interval})"
    previous_window = f"floor((prev_selected_t+TB*1e-6)/{interval})"
    filters = (
        f"select='gte(t,0)*if(isnan(prev_selected_t),1,gt({window},{previous_window}))',"
        "showinfo=checksum=0"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".frames-", dir=output.parent) as temporary:
        staged = Path(temporary) / "result"
        staged.mkdir()
        result = run_tool(
            settings.ffmpeg_path,
            [
                "-hide_banner",
                "-nostats",
                "-loglevel",
                "info",
                "-xerror",
                "-copyts",
                "-start_at_zero",
                "-i",
                str(source),
                "-map",
                "0:V:0",
                "-vf",
                filters,
                "-fps_mode",
                "passthrough",
                "-c:v",
                "mjpeg",
                "-q:v",
                "2",
                "-f",
                "image2",
                "frame-%06d.jpg",
            ],
            timeout=300,
            cwd=staged,
        )
        frames = validate_frames(staged, frame_timestamps(result.stderr))
        manifest = {
            "durationSeconds": video.duration_seconds,
            "width": video.width,
            "height": video.height,
            "frames": frames,
        }
        (staged / "frames.json").write_text(
            json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        # 同一文件系统内整体发布，避免失败时留下半套结果。
        check_output_directory(output)
        try:
            staged.rename(output)
        except FileExistsError as exc:
            raise VideoError(f"输出目录已有结果 {output}，请指定新的输出目录") from exc

    logger.info(
        "抽帧完成，共 %d 张，耗时 %.2f 秒", len(frames), perf_counter() - started_at
    )
    return frames
