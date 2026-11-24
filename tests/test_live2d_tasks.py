import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app.live2d import generate
from app.main import app

ROUTE = "/internal/v1/live2d/tasks"
REQUEST = {
    "taskId": "live2d-01",
    "projectId": 42,
    "modelName": "末班车角色",
    "sourceImageRef": "actor.png",
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    image = pytest.importorskip("PIL.Image")
    image.new("RGBA", (4, 4), "#c06080").save(tmp_path / "actor.png")
    monkeypatch.setenv("LIVE2D_INPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("LIVE2D_OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("LIVE2D_CONVERTER_ARGV", "[]")
    monkeypatch.setenv("LIVE2D_CORE_LIBRARY", str(tmp_path / "not-installed.dylib"))
    monkeypatch.setenv("LIVE2D_WEB_CORE_JS", str(tmp_path / "not-installed.js"))
    return tmp_path


def wait_for_task(client: TestClient) -> dict:
    deadline = monotonic() + 4
    while monotonic() < deadline:
        response = client.get(f"{ROUTE}/{REQUEST['taskId']}")
        assert response.status_code == 200
        state = response.json()
        if state["status"] in {"SUCCEEDED", "FAILED"}:
            return state
        sleep(0.01)
    pytest.fail("Live2D 任务未在期限内完成")


def test_idempotent_task_publishes_zip_after_mock_core_validation(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = workspace / "test-core.dylib"
    library.touch()
    monkeypatch.setenv("LIVE2D_CORE_LIBRARY", str(library))
    monkeypatch.setenv("LIVE2D_CONVERTER_ARGV", '["test-converter"]')
    commands = []

    def fake_tool(
        argv: list[str], timeout: int, job_id: str, *, env: dict[str, str] | None = None
    ) -> None:
        commands.append(argv[0:3])
        if argv[0] != "test-converter":
            assert argv[1:3] == ["-m", "app.live2d.core"]
            assert Path(argv[-1]).is_file()
            assert not any(
                path.name.startswith("live2d-01")
                for path in (workspace / "output").iterdir()
            )
            return

        assert env is not None and env["LIVE2D_MODEL_NAME"] == REQUEST["modelName"]
        model = Path(argv[argv.index("--output") + 1]) / "rig"
        (model / "tex").mkdir(parents=True)
        (model / "actor.moc3").write_bytes(b"test sentinel")
        (model / "tex" / "0.png").write_bytes((workspace / "actor.png").read_bytes())
        (model / "idle.motion3.json").write_text("{}")
        (model / "actor.model3.json").write_text(
            json.dumps(
                {
                    "Version": 3,
                    "FileReferences": {
                        "Moc": "actor.moc3",
                        "Textures": ["tex/0.png"],
                        "Motions": {"Idle": [{"File": "idle.motion3.json"}]},
                    },
                }
            )
        )

    monkeypatch.setattr(generate, "run_tool", fake_tool)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=4) as pool:
        assert client.get(f"{ROUTE}/missing").status_code == 404
        responses = list(pool.map(lambda _: client.post(ROUTE, json=REQUEST), range(4)))
        assert all(response.status_code == 202 for response in responses)
        assert any(response.json()["status"] == "QUEUED" for response in responses)
        assert client.post(ROUTE, json={**REQUEST, "projectId": 43}).status_code == 409
        state = wait_for_task(client)
        assert client.post(ROUTE, json=REQUEST).json() == state
    assert len(commands) == 2
    assert state["status"] == "SUCCEEDED", state
    assert state["result"]["manifestPath"] == "rig/actor.model3.json"
    with ZipFile(workspace / "output" / state["result"]["artifactRef"]) as artifact:
        assert sorted(artifact.namelist()) == [
            "rig/actor.moc3",
            "rig/actor.model3.json",
            "rig/idle.motion3.json",
            "rig/tex/0.png",
        ]


@pytest.mark.parametrize(
    ("mode", "expected_code", "stage"),
    [
        ("missing", "CONVERTER_UNAVAILABLE", "GENERATING"),
        ("exit", "CONVERSION_FAILED", "GENERATING"),
        ("psd", "RUNTIME_NOT_EXPORTED", "VALIDATING"),
        ("escape", "RUNTIME_INVALID", "VALIDATING"),
        ("no-core", "CUBISM_CORE_UNAVAILABLE", "VALIDATING"),
    ],
)
def test_failed_job_preserves_stage_and_publishes_nothing(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_code: str,
    stage: str,
) -> None:
    if mode != "missing":
        monkeypatch.setenv("LIVE2D_CONVERTER_ARGV", '["test-converter"]')

    def fake_tool(
        argv: list[str], timeout: int, job_id: str, *, env: dict[str, str] | None = None
    ) -> None:
        if mode == "exit":
            raise subprocess.CalledProcessError(1, argv)
        output = Path(argv[-1])
        output.mkdir()
        (output / "layers.psd").touch()
        if mode == "no-core":
            (output / "actor.moc3").write_bytes(b"test sentinel")
            (output / "actor.png").write_bytes((workspace / "actor.png").read_bytes())
            (output / "actor.model3.json").write_text(
                '{"Version":3,"FileReferences":{"Moc":"actor.moc3","Textures":["actor.png"]}}'
            )
        if mode == "escape":
            (output / "invalid.model3.json").write_text(
                '{"Version":3,"FileReferences":{"Moc":"../bad.moc3"}}'
            )

    monkeypatch.setattr(generate, "run_tool", fake_tool)
    with TestClient(app) as client:
        response = client.post(ROUTE, json=REQUEST)
        assert response.status_code == 202
        state = wait_for_task(client)
    assert state["status"] == "FAILED"
    assert state["error"]["code"] == expected_code
    assert state["stage"] == state["error"]["stage"] == stage
    assert state["result"] is None
    assert not list((workspace / "output").iterdir())


def test_events_socket_pushes_terminal_state(workspace: Path) -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/internal/v1/live2d/tasks/events") as socket:
            created = client.post(ROUTE, json=REQUEST)
            assert created.status_code == 202
            state = {"status": "QUEUED"}
            while state["status"] not in {"SUCCEEDED", "FAILED"}:
                state = socket.receive_json()
            assert state["taskId"] == REQUEST["taskId"]
            assert state["status"] == "FAILED"
            assert state["error"]["code"]


def test_rejects_outside_references_and_missing_images(workspace: Path) -> None:
    (workspace / "outside.png").symlink_to(workspace.parent / "outside.png")
    with TestClient(app) as client:
        for reference in ["../actor.png", "https://example.com/a.png", "outside.png"]:
            response = client.post(ROUTE, json={**REQUEST, "sourceImageRef": reference})
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "SOURCE_IMAGE_REF_INVALID"
        response = client.post(ROUTE, json={**REQUEST, "sourceImageRef": "absent.png"})
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "SOURCE_IMAGE_NOT_FOUND"
