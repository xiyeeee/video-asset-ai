import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from threading import Event, Lock

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

ENDPOINT = "/internal/v1/character-extraction/tasks"
REQUEST = {"taskId": "task-001", "sourceVideoRef": "uploads/episode.mp4"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    input_root = tmp_path / "input"
    (input_root / "uploads").mkdir(parents=True)
    (input_root / REQUEST["sourceVideoRef"]).touch()
    (tmp_path / "outside.mp4").touch()
    (input_root / "escape.mp4").symlink_to(tmp_path / "outside.mp4")
    monkeypatch.setenv("CHARACTER_INPUT_ROOT", str(input_root))
    monkeypatch.setenv("CHARACTER_OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("CHARACTER_FRAME_INTERVAL_SECONDS", "2")

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as session,
    ):
        yield session


async def await_terminal(client: AsyncClient, task_id: str) -> dict:
    async with asyncio.timeout(5):
        while True:
            response = await client.get(f"{ENDPOINT}/{task_id}")
            assert response.status_code == 200
            state = response.json()
            if state["status"] in {"SUCCEEDED", "FAILED"}:
                return state
            await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_concurrent_submissions_execute_once_and_other_task_waits(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = Event()
    release = Event()
    calls_lock = Lock()
    calls: list[str] = []

    def run(request: TaskRequest, source: Path, report: ReportStage) -> TaskResult:
        assert source.name == "episode.mp4"
        assert request.frame_interval_seconds == 2
        with calls_lock:
            calls.append(request.task_id)
        report(TaskStage.EXTRACTING_FRAMES, TaskProgress())
        started.set()
        assert release.wait(10)
        report(
            TaskStage.WRITING_RESULTS,
            TaskProgress(frame_count=1, detected_character_count=0),
        )
        return TaskResult(
            manifest_ref="result/characters.json", character_asset_count=0
        )

    monkeypatch.setattr(app.state.character_tasks, "run_task", run)
    try:
        requests = [REQUEST, {**REQUEST, "frameIntervalSeconds": 2}] * 4
        responses = await asyncio.gather(
            *(client.post(ENDPOINT, json=request) for request in requests)
        )
        assert all(response.status_code == 202 for response in responses)
        assert any(response.json()["status"] == "QUEUED" for response in responses)
        assert all(response.headers["X-Request-ID"] for response in responses)
        assert await asyncio.to_thread(started.wait, 5)

        running = (await client.get(f"{ENDPOINT}/task-001")).json()
        assert running["status"] == "RUNNING"
        assert running["stage"] == "EXTRACTING_FRAMES"

        conflict = await client.post(
            ENDPOINT, json={**REQUEST, "frameIntervalSeconds": 3}
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "TASK_CONFLICT"

        queued = await client.post(ENDPOINT, json={**REQUEST, "taskId": "task-002"})
        assert queued.json()["status"] == "QUEUED"
        assert (await client.get(f"{ENDPOINT}/task-002")).json()["status"] == "QUEUED"
        with calls_lock:
            assert calls == ["task-001"]
    finally:
        release.set()

    first = await await_terminal(client, "task-001")
    assert first["status"] == "SUCCEEDED"
    assert first["result"] == {
        "manifestRef": "result/characters.json",
        "characterAssetCount": 0,
    }
    assert (await await_terminal(client, "task-002"))["status"] == "SUCCEEDED"
    assert (await client.post(ENDPOINT, json=REQUEST)).json() == first
    with calls_lock:
        assert calls == ["task-001", "task-002"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reference",
    [
        "../outside.mp4",
        "/tmp/episode.mp4",
        "escape.mp4",
        "uploads/../episode.mp4",
        "C:\\video.mp4",
    ],
)
async def test_rejects_unsafe_file_references(
    client: AsyncClient, reference: str
) -> None:
    response = await client.post(
        ENDPOINT, json={**REQUEST, "sourceVideoRef": reference}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SOURCE_VIDEO_REF_INVALID"
    assert (await client.get(f"{ENDPOINT}/task-001")).status_code == 404


@pytest.mark.anyio
async def test_invalid_parameters_and_missing_resources(client: AsyncClient) -> None:
    for updates in (
        {"taskId": "../task"},
        {"frameIntervalSeconds": 0},
        {"frameIntervalSeconds": True},
    ):
        response = await client.post(ENDPOINT, json={**REQUEST, **updates})
        assert (response.status_code, response.json()["detail"]["code"]) == (
            422,
            "REQUEST_INVALID",
        )

    missing_source = await client.post(
        ENDPOINT, json={**REQUEST, "sourceVideoRef": "missing.mp4"}
    )
    assert (missing_source.status_code, missing_source.json()["detail"]["code"]) == (
        404,
        "SOURCE_VIDEO_NOT_FOUND",
    )

    missing_task = await client.get(f"{ENDPOINT}/task-001")
    assert (missing_task.status_code, missing_task.json()["detail"]["code"]) == (
        404,
        "TASK_NOT_FOUND",
    )


@pytest.mark.anyio
async def test_processing_failure_exposes_stable_error_and_no_result(
    client: AsyncClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    response = await client.post(ENDPOINT, json=REQUEST)
    assert response.status_code == 202
    state = await await_terminal(client, "task-001")

    # 空 MP4 经真实 FFprobe 失败，异常详情只写入服务日志。
    assert state["status"] == "FAILED"
    assert state["stage"] == "EXTRACTING_FRAMES"
    assert state["result"] is None
    assert state["error"] == {
        "code": "FRAME_EXTRACTION_FAILED",
        "message": "视频抽帧失败，请检查源视频",
    }
    assert "taskId=task-001" in caplog.text
    assert not list((tmp_path / "output").iterdir())
    assert (await client.post(ENDPOINT, json=REQUEST)).json() == state
