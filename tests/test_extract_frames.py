import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.video.ffmpeg import VideoError, extract_frames


@pytest.fixture
def ffmpeg_tools() -> Settings:
    if not (ffmpeg := shutil.which("ffmpeg")) or not (
        ffprobe := shutil.which("ffprobe")
    ):
        pytest.skip("集成测试需要系统 FFmpeg 和 FFprobe")
    return Settings(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)


@pytest.fixture
def sample_video(tmp_path: Path, ffmpeg_tools: Settings) -> Path:
    source = tmp_path / "sample.mp4"
    # 音轨从 0 秒开始，视频流首帧在 0.5 秒，覆盖播放器时间与视频流时间的区别。
    subprocess.run(
        [
            ffmpeg_tools.ffmpeg_path,
            "-v",
            "error",
            "-nostdin",
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=4:duration=2.75",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono:d=3.25",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    return source


def test_extracts_real_frames_with_playback_timestamps(
    tmp_path: Path, sample_video: Path, ffmpeg_tools: Settings
) -> None:
    output = tmp_path / "frames"
    output.mkdir()
    frames = extract_frames(sample_video, output, 0.6, ffmpeg_tools)

    manifest = json.loads((output / "frames.json").read_text(encoding="utf-8"))
    assert manifest == {
        "durationSeconds": 3.25,
        "width": 96,
        "height": 64,
        "frames": frames,
    }
    assert [frame["timestampMs"] for frame in frames] == [
        500,
        750,
        1250,
        2000,
        2500,
        3000,
    ]
    assert {frame["path"] for frame in frames} == {
        image.name for image in output.glob("*.jpg")
    }


def test_existing_results_are_preserved(tmp_path: Path) -> None:
    source = tmp_path / "input.mp4"
    source.touch()
    existing_result = tmp_path / "frames.json"
    existing_result.write_text("原有结果", encoding="utf-8")

    with pytest.raises(VideoError, match="输出路径已存在"):
        extract_frames(source, tmp_path, 2, Settings())

    assert existing_result.read_text(encoding="utf-8") == "原有结果"


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_interval(tmp_path: Path, interval: float) -> None:
    with pytest.raises(VideoError, match="抽帧间隔必须是有限正数"):
        extract_frames(
            tmp_path / "input.mp4", tmp_path / "output", interval, Settings()
        )


def test_tool_failure_leaves_no_results(
    tmp_path: Path, sample_video: Path, ffmpeg_tools: Settings
) -> None:
    output = tmp_path / "frames"
    settings = ffmpeg_tools.model_copy(
        update={"ffmpeg_path": str(tmp_path / "missing")}
    )

    with pytest.raises(VideoError, match="找不到可执行工具"):
        extract_frames(sample_video, output, 2, settings)

    assert not output.exists()
    assert not list(tmp_path.glob(".frames-*"))


def test_rejects_audio_only(tmp_path: Path, ffmpeg_tools: Settings) -> None:
    source = tmp_path / "audio.wav"
    output = tmp_path / "frames"
    subprocess.run(
        [
            ffmpeg_tools.ffmpeg_path,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=d=0.5",
            str(source),
        ],
        check=True,
        timeout=30,
    )

    with pytest.raises(VideoError, match="没有视频流"):
        extract_frames(source, output, 2, ffmpeg_tools)

    assert not output.exists()
