import asyncio
import shutil
import subprocess
import wave
from pathlib import Path
from threading import Event

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pipeline.character_extraction import ReportStage
from app.pipeline.character_task_models import (
    TaskProgress,
    TaskRequest,
    TaskResult,
    TaskStage,
)

ENDPOINT = "/internal/v1/asset-extraction/tasks"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def source_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if not (ffmpeg := shutil.which("ffmpeg")) or not (
        ffprobe := shutil.which("ffprobe")
    ):
        pytest.skip("音频集成测试需要 FFmpeg 和 FFprobe")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for filename, format_args in (
        (
            "audio.mp4",
            ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:a", "aac"],
        ),
        (
            "silent.mp4",
            ["-f", "lavfi", "-i", "color=size=32x32:duration=0.5", "-c:v", "mpeg4"],
        ),
    ):
        subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", *format_args, str(inputs / filename)],
            check=True,
            timeout=10,
        )
    output = tmp_path / "output"
    monkeypatch.setenv("CHARACTER_INPUT_ROOT", str(inputs))
    monkeypatch.setenv("CHARACTER_OUTPUT_ROOT", str(output))
    monkeypatch.setenv("FFMPEG_PATH", ffmpeg)
    monkeypatch.setenv("FFPROBE_PATH", ffprobe)
    return output


async def wait_for_status(
    client: AsyncClient, task_id: str, status: str, branch: str | None = None
) -> dict:
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"{ENDPOINT}/{task_id}")
            assert response.status_code == 200
            state = response.json()
            current = state["branches"][branch] if branch else state
            if current["status"] == status:
                return state
            assert current["status"] in {"QUEUED", "RUNNING"}, state
            await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_audio_finishes_while_characters_are_running(
    source_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = Event()
    calls: list[str] = []

    def run_characters(
        task: TaskRequest, source: Path, report: ReportStage
    ) -> TaskResult:
        calls.append(task.task_id)
        report(TaskStage.DETECTING_CHARACTERS, TaskProgress(frame_count=1))
        assert release.wait(15)
        return TaskResult(
            manifest_ref="character/characters.json", character_asset_count=1
        )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        monkeypatch.setattr(app.state.asset_tasks, "run_characters", run_characters)
        request = {
            "taskId": "parallel",
            "sourceVideoRef": "audio.mp4",
            "outputs": ["CHARACTERS", "AUDIO"],
        }
        try:
            first = await client.post(ENDPOINT, json=request)
            assert first.status_code == 202 and first.json()["status"] == "QUEUED"
            duplicate = await client.post(
                ENDPOINT, json={**request, "outputs": ["AUDIO", "CHARACTERS"]}
            )
            assert duplicate.status_code == 202
            conflict = await client.post(
                ENDPOINT, json={**request, "outputs": ["CHARACTERS"]}
            )
            assert conflict.status_code == 409
            await wait_for_status(client, "parallel", "RUNNING", "CHARACTERS")
            state = await wait_for_status(client, "parallel", "SUCCEEDED", "AUDIO")
            assert state["status"] == "RUNNING"
            assert state["branches"]["CHARACTERS"]["stage"] == "DETECTING_CHARACTERS"
            audio = source_files / state["branches"]["AUDIO"]["result"]["audioRef"]
            with wave.open(str(audio), "rb") as result:
                assert result.getnframes() > 0
        finally:
            release.set()

        state = await wait_for_status(client, "parallel", "SUCCEEDED")
        assert (await client.post(ENDPOINT, json=request)).json() == state
        assert calls == ["parallel"]
        assert len(list(source_files.glob("*/audio.wav"))) == 1


@pytest.mark.anyio
async def test_missing_audio_preserves_character_result_and_audio_only_skips_characters(
    source_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def run_characters(
        task: TaskRequest, source: Path, report: ReportStage
    ) -> TaskResult:
        calls.append(task.task_id)
        report(TaskStage.WRITING_RESULTS, TaskProgress(frame_count=1))
        return TaskResult(
            manifest_ref="character/characters.json", character_asset_count=1
        )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        monkeypatch.setattr(app.state.asset_tasks, "run_characters", run_characters)
        request = {
            "taskId": "partial",
            "sourceVideoRef": "silent.mp4",
            "outputs": ["CHARACTERS", "AUDIO"],
        }
        assert (await client.post(ENDPOINT, json=request)).status_code == 202
        state = await wait_for_status(client, "partial", "PARTIALLY_SUCCEEDED")
        assert state["branches"]["CHARACTERS"]["result"]["characterAssetCount"] == 1
        assert state["branches"]["AUDIO"]["error"]["code"] == "AUDIO_STREAM_NOT_FOUND"
        assert not list(source_files.glob("partial-audio-*"))

        audio_only = {
            "taskId": "audio-only",
            "sourceVideoRef": "audio.mp4",
            "outputs": ["AUDIO"],
        }
        assert (await client.post(ENDPOINT, json=audio_only)).status_code == 202
        result = await wait_for_status(client, "audio-only", "SUCCEEDED")
        assert set(result["branches"]) == {"AUDIO"}
        assert (
            await client.post(ENDPOINT, json={**audio_only, "frameIntervalSeconds": 9})
        ).json() == result
        assert calls == ["partial"]
