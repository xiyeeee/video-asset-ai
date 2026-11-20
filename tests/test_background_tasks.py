from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from app.backgrounds.select import DetectionStatus, ReviewStatus, review_status
from app.main import app
from app.pipeline.asset_task_models import ShotResult, ShotStage
from app.video.ffmpeg import VideoError

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL.Image import Image

    from app.characters.extract import Detection
    from app.core.config import Settings

ENDPOINT = "/internal/v1/asset-extraction/tasks"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def output_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pytest.importorskip("PIL")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("背景任务测试需要 FFmpeg 和 FFprobe")
    source = tmp_path / "input"
    source.mkdir()
    args = [ffmpeg, "-v", "error", "-nostdin"]
    for color, duration in (("red", 1), ("blue", 0.2), ("white", 1)):
        args += [
            "-f",
            "lavfi",
            "-i",
            f"color={color}:size=96x64:rate=10:duration={duration}",
        ]
    subprocess.run(
        [
            *args,
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[video]",
            "-map",
            "[video]",
            "-c:v",
            "mpeg4",
            str(source / "episode.mp4"),
        ],
        timeout=15,
        check=True,
    )
    output = tmp_path / "output"
    monkeypatch.setenv("CHARACTER_INPUT_ROOT", str(source))
    monkeypatch.setenv("CHARACTER_OUTPUT_ROOT", str(output))
    monkeypatch.setenv("FFMPEG_PATH", ffmpeg)
    monkeypatch.setenv("FFPROBE_PATH", ffprobe)
    return output


async def await_terminal(client: AsyncClient, task_id: str) -> dict:
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"{ENDPOINT}/{task_id}")
            assert response.status_code == 200
            state = response.json()
            if state["status"] not in {"QUEUED", "RUNNING"}:
                return state
            await asyncio.sleep(0.02)


def detect_blue_foreground(image: Image) -> list[Detection]:
    from app.characters.extract import Detection

    red, _, blue = image.getpixel((1, 1))
    if blue > red:
        return [Detection(box=(0, 0, 40, 64), score=0.8)]
    return []


@pytest.mark.anyio
async def test_reuses_same_task_and_existing_shot_manifest(
    output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        monkeypatch.setattr(
            app.state.asset_tasks, "detect_people", detect_blue_foreground
        )
        request = {
            "taskId": "joint",
            "sourceVideoRef": "episode.mp4",
            "outputs": ["BACKGROUND", "SHOTS"],
        }
        first = await client.post(ENDPOINT, json=request)
        assert first.status_code == 202
        assert first.json()["branches"]["BACKGROUND"]["status"] == "QUEUED"
        state = await await_terminal(client, "joint")
        assert state["status"] == "SUCCEEDED", state
        assert (
            await client.post(
                ENDPOINT, json={**request, "outputs": ["SHOTS", "BACKGROUND"]}
            )
        ).json() == state
        result = state["branches"]["BACKGROUND"]["result"]
        assert result["backgroundCount"] == 3
        manifest_path = output_root / result["manifestRef"]
        manifest = json.loads(manifest_path.read_text())
        assert manifest["durationMs"] == 2200
        assert [
            (item["startMs"], item["endMs"], item["timestampMs"])
            for item in manifest["backgrounds"]
        ] == [(0, 1000, 500), (1000, 1200, 1100), (1200, 2200, 1700)]
        assert [item["personStatus"] for item in manifest["backgrounds"]] == [
            "NOT_DETECTED",
            "DETECTED",
            "NOT_DETECTED",
        ]
        for item in manifest["backgrounds"]:
            assert item["method"] == "SOURCE_FRAME"
            assert item["subtitleStatus"] == "UNKNOWN"
            assert item["reviewStatus"] == "NEEDS_REVIEW"
            assert (
                (manifest_path.parent / item["imagePath"])
                .read_bytes()
                .startswith(b"\xff\xd8")
            )

        def fail_scan(source: Path, settings: Settings) -> None:
            pytest.fail("有效外部清单应直接复用")

        monkeypatch.setattr(
            "app.pipeline.background_extraction.detect_shots", fail_scan
        )
        reference = state["branches"]["SHOTS"]["result"]["manifestRef"]
        repeat = {
            **request,
            "taskId": "reuse",
            "outputs": ["BACKGROUND"],
            "shotManifestRef": reference,
        }
        assert (await client.post(ENDPOINT, json=repeat)).status_code == 202
        reused = await await_terminal(client, "reuse")
        assert reused["status"] == "SUCCEEDED"
        reused_manifest = (
            output_root / reused["branches"]["BACKGROUND"]["result"]["manifestRef"]
        )
        assert json.loads(reused_manifest.read_text()) == manifest
        changed = await client.post(
            ENDPOINT, json={**repeat, "shotManifestRef": "other/shots.json"}
        )
        assert changed.status_code == 409
        ambiguous = await client.post(
            ENDPOINT, json={**request, "shotManifestRef": reference}
        )
        assert ambiguous.status_code == 422


@pytest.mark.anyio
async def test_uses_internal_shots_and_rejects_unsafe_manifest(
    output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        monkeypatch.setattr(
            app.state.asset_tasks, "detect_people", detect_blue_foreground
        )
        independent = {
            "taskId": "auto",
            "sourceVideoRef": "episode.mp4",
            "outputs": ["BACKGROUND"],
        }
        assert (await client.post(ENDPOINT, json=independent)).status_code == 202
        state = await await_terminal(client, "auto")
        assert state["status"] == "SUCCEEDED"
        assert state["branches"]["BACKGROUND"]["result"]["backgroundCount"] == 3
        assert not list(output_root.glob("auto-shots-*"))

        outside = output_root.parent / "outside.json"
        outside.write_text("{}")
        (output_root / "linked-shots.json").symlink_to(outside)
        for index, ref in enumerate(("../outside.json", "linked-shots.json"), 1):
            task_id = f"invalid-{index}"
            body = {**independent, "taskId": task_id, "shotManifestRef": ref}
            assert (await client.post(ENDPOINT, json=body)).status_code == 202
            invalid = await await_terminal(client, task_id)
            assert (
                invalid["branches"]["BACKGROUND"]["error"]["code"]
                == "BACKGROUND_SHOT_MANIFEST_INVALID"
            )


@pytest.mark.anyio
async def test_background_fails_when_same_task_shots_fail(
    output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_shots(
        task_id: str,
        source: Path,
        output: Path,
        settings: Settings,
        report: Callable[[ShotStage], None],
    ) -> ShotResult:
        report(ShotStage.DETECTING_SHOTS)
        raise VideoError("模拟切镜失败")

    monkeypatch.setattr("app.pipeline.asset_tasks.extract_shots", fail_shots)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        body = {
            "taskId": "fail",
            "sourceVideoRef": "episode.mp4",
            "outputs": ["BACKGROUND", "SHOTS"],
        }
        assert (await client.post(ENDPOINT, json=body)).status_code == 202
        result = await await_terminal(client, "fail")
        assert result["status"] == "FAILED"
        assert (
            result["branches"]["BACKGROUND"]["error"]["code"]
            == "BACKGROUND_SHOTS_FAILED"
        )
        assert not output_root.exists()


def test_only_checked_frames_can_be_clean_candidates() -> None:
    assert (
        review_status(DetectionStatus.NOT_DETECTED, DetectionStatus.UNKNOWN)
        is ReviewStatus.NEEDS_REVIEW
    )
    assert (
        review_status(DetectionStatus.NOT_DETECTED, DetectionStatus.DETECTED)
        is ReviewStatus.NEEDS_REVIEW
    )
    assert (
        review_status(DetectionStatus.NOT_DETECTED, DetectionStatus.NOT_DETECTED)
        is ReviewStatus.CLEAN_CANDIDATE
    )
