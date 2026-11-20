from pathlib import Path

from app.core.config import Settings
from app.video.ffmpeg import VideoError, run_tool


class AudioStreamNotFound(VideoError):
    """源视频没有可提取的音轨。"""


def extract_audio(source: Path, output: Path, settings: Settings) -> None:
    streams = run_tool(
        settings.ffprobe_path,
        [
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(source),
        ],
        timeout=30,
    )
    if not streams.stdout.strip():
        raise AudioStreamNotFound("源视频没有音轨")
    run_tool(
        settings.ffmpeg_path,
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-n",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        timeout=300,
    )
    # 用 FFprobe 校验实际音频数据，也兼容多声道 WAV。
    packet = run_tool(
        settings.ffprobe_path,
        [
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-read_intervals",
            "%+#1",
            "-show_entries",
            "packet=size",
            "-of",
            "csv=p=0",
            str(output),
        ],
        timeout=30,
    )
    if not packet.stdout.strip().isdigit() or int(packet.stdout.strip()) <= 0:
        raise VideoError("导出的 WAV 没有采样数据")
