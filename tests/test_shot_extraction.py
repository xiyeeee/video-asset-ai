import asyncio
import json
import shutil
import subprocess
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import app
from app.pipeline.asset_task_models import ShotStage
from app.pipeline.shot_extraction import extract_shots
from app.video.ffmpeg import VideoError
from app.video.shots import VideoFrame

ENDPOINT = "/internal/v1/asset-extraction/tasks"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def media_tools() -> Settings:
    if not (ffmpeg := shutil.which("ffmpeg")) or not (
        ffprobe := shutil.which("ffprobe")
    ):
        pytest.skip("分镜集成测试需要 FFmpeg 和 FFprobe")
    return Settings(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)


def generate_video(
    path: Path,
    tools: Settings,
    segments: list[tuple[str, float]],
    *,
    audio: bool = False,
) -> None:
    arguments = [tools.ffmpeg_path, "-v", "error", "-nostdin"]
    for color, seconds in segments:
        arguments += [
            "-f",
            "lavfi",
            "-i",
            f"color={color}:size=96x64:rate=10:duration={seconds}",
        ]
    if audio:
        duration = sum(seconds for _, seconds in segments)
        arguments += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    inputs = "".join(f"[{index}:v:0]" for index in range(len(segments)))
    arguments += [
        "-filter_complex",
        f"{inputs}concat=n={len(segments)}:v=1:a=0[out]",
        "-map",
        "[out]",
        "-c:v",
        "mpeg4",
    ]
    if audio:
        arguments += ["-map", f"{len(segments)}:a:0", "-c:a", "aac"]
    subprocess.run([*arguments, str(path)], check=True, timeout=15)


def source_timestamps(path: Path, tools: Settings) -> list[int]:
    data = json.loads(
        subprocess.check_output(
            [
                tools.ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "V:0",
                "-show_entries",
                "frame=best_effort_timestamp_time:format=start_time",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            timeout=15,
        )
    )
    start = Decimal(data["format"]["start_time"])
    return [
        int(
            ((Decimal(frame["best_effort_timestamp_time"]) - start) * 1000).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        for frame in data["frames"]
    ]


@pytest.mark.parametrize(
    ("segments", "boundaries"),
    [
        ([("red", 2)], [0, 2000]),
        ([("red", 1), ("blue", 0.2), ("white", 1)], [0, 1000, 1200, 2200]),
        ([("red", 1), ("white", 0.1), ("red", 1)], [0, 2100]),
    ],
)
def test_extract_shots_cuts_and_actual_previews(
    tmp_path: Path,
    media_tools: Settings,
    segments: list[tuple[str, float]],
    boundaries: list[int],
) -> None:
    source = tmp_path / "source.mp4"
    generate_video(source, media_tools, segments)
    stages: list[ShotStage] = []
    result = extract_shots(
        "cut", source, tmp_path / "published", media_tools, stages.append
    )
    manifest_path = tmp_path / "published" / result.manifest_ref
    manifest = json.loads(manifest_path.read_text())
    frames = source_timestamps(source, media_tools)
    assert stages == list(ShotStage)
    assert manifest["schemaVersion"] == 1
    assert manifest["durationMs"] == boundaries[-1]
    assert result.shot_count == len(manifest["shots"]) == len(boundaries) - 1
    for index, shot in enumerate(manifest["shots"]):
        start, end = boundaries[index : index + 2]
        assert shot["shotIndex"] == index + 1
        assert (shot["startMs"], shot["endMs"]) == (start, end)
        wanted = min(
            (frame for frame in frames if start <= frame < end),
            key=lambda timestamp: abs(timestamp - (start + end) / 2),
        )
        assert shot["previewTimestampMs"] == wanted
        preview = manifest_path.parent / shot["previewPath"]
        assert preview.read_bytes().startswith(b"\xff\xd8")


def test_preview_uses_playback_origin_with_delayed_variable_rate_video(
    tmp_path: Path, media_tools: Settings
) -> None:
    source = tmp_path / "delayed.mp4"
    subprocess.run(
        [
            media_tools.ffmpeg_path,
            "-v",
            "error",
            "-nostdin",
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=4:duration=1.5",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono:d=2",
            "-vf",
            "select='not(eq(n,2))'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=15,
    )
    settings = media_tools.model_copy(update={"shot_scene_threshold": 100})
    output = tmp_path / "published"
    result = extract_shots("delayed", source, output, settings, lambda _: None)
    manifest = json.loads((output / result.manifest_ref).read_text())
    assert manifest["durationMs"] == 2000 and result.shot_count == 1
    frames = source_timestamps(source, media_tools)
    assert frames[0] == 500 and 1000 not in frames
    assert manifest["shots"][0]["previewTimestampMs"] == min(
        frames, key=lambda timestamp: abs(timestamp - 1000)
    )


def test_bottom_subtitle_overlay_does_not_split_a_shot(
    tmp_path: Path, media_tools: Settings
) -> None:
    source = tmp_path / "subtitle-overlay.mp4"
    subprocess.run(
        [
            media_tools.ffmpeg_path,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=black:size=96x100:rate=10:duration=2",
            "-vf",
            "drawbox=x=0:y=81:w=96:h=19:color=white:t=fill:enable='gte(t,1)'",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
        timeout=15,
    )
    output = tmp_path / "published"
    result = extract_shots("subtitles", source, output, media_tools, lambda _: None)
    assert result.shot_count == 1

    full_frame = media_tools.model_copy(update={"shot_ignore_bottom_fraction": 0})
    with_overlay = extract_shots(
        "full-frame", source, output, full_frame, lambda _: None
    )
    assert with_overlay.shot_count == 2


async def wait_for_terminal(client: AsyncClient, task_id: str) -> dict:
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"{ENDPOINT}/{task_id}")
            assert response.status_code == 200
            state = response.json()
            if state["status"] not in {"QUEUED", "RUNNING"}:
                return state
            await asyncio.sleep(0.02)


@pytest.mark.anyio
async def test_shots_http_idempotency_and_partial_failure(
    tmp_path: Path, media_tools: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    generate_video(source, media_tools, [("red", 1), ("blue", 1)], audio=True)
    output = tmp_path / "published"
    monkeypatch.setenv("CHARACTER_INPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("CHARACTER_OUTPUT_ROOT", str(output))
    monkeypatch.setenv("FFMPEG_PATH", media_tools.ffmpeg_path)
    monkeypatch.setenv("FFPROBE_PATH", media_tools.ffprobe_path)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        request = {
            "taskId": "both",
            "sourceVideoRef": source.name,
            "outputs": ["SHOTS", "AUDIO"],
        }
        first = await client.post(ENDPOINT, json=request)
        assert (
            first.status_code == 202
            and first.json()["branches"]["SHOTS"]["stage"] is None
        )
        duplicate = await client.post(
            ENDPOINT, json={**request, "frameIntervalSeconds": 9}
        )
        assert duplicate.status_code == 202
        state = await wait_for_terminal(client, "both")
        assert state["status"] == "SUCCEEDED"
        shots = state["branches"]["SHOTS"]
        assert shots["stage"] == "WRITING_RESULTS" and shots["progress"] is None
        assert shots["result"]["shotCount"] == 2
        assert (output / shots["result"]["manifestRef"]).is_file()
        assert (await client.post(ENDPOINT, json=request)).json() == state
        assert len(list(output.glob("both-shots-*"))) == 1

        def fail_previews(
            source: Path,
            directory: Path,
            selected: list[VideoFrame],
            settings: Settings,
        ) -> None:
            raise VideoError("代表图生成失败")

        monkeypatch.setattr(
            "app.pipeline.shot_extraction.extract_shot_previews", fail_previews
        )
        assert (
            await client.post(ENDPOINT, json={**request, "taskId": "partial"})
        ).status_code == 202
        partial = await wait_for_terminal(client, "partial")
        assert partial["status"] == "PARTIALLY_SUCCEEDED"
        assert partial["branches"]["SHOTS"]["error"]["code"] == "SHOT_PREVIEW_FAILED"
        assert partial["branches"]["SHOTS"]["result"] is None
        assert (output / partial["branches"]["AUDIO"]["result"]["audioRef"]).is_file()
        assert not list(output.glob("partial-shots-*"))
