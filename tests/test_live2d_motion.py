import hashlib
import importlib
import json
import shutil
import struct
import wave
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.live2d.compile_motion import compile_motion
from app.live2d.motion_capabilities import ACTION_PARAMETERS
from app.live2d.motion_models import (
    Capabilities,
    Capability,
    MotionError,
    MotionRequest,
    Parameter,
)
from app.main import app

ROUTE = "/internal/v1/live2d/motion-tasks"


@pytest.fixture
def model() -> Capabilities:
    ranges = {
        "ParamMouthForm": (-1.0, 1.0, 0.0),
        "ParamMouthOpenY": (0.0, 1.0, 0.0),
        "ParamAngleY": (-30.0, 30.0, 0.0),
        "ParamEyeLOpen": (0.0, 1.0, 1.0),
        "ParamEyeROpen": (0.0, 1.0, 1.0),
    }
    return Capabilities(
        model_version_id="421",
        moc_version=1,
        moc_sha256="a" * 64,
        parameters=[
            Parameter(
                id=key,
                min=lo,
                max=hi,
                default_value=neutral,
                active=True,
                active_decreasing=lo < neutral,
                active_increasing=hi > neutral,
            )
            for key, (lo, hi, neutral) in ranges.items()
        ],
        capabilities=[
            Capability(
                id=key,
                supported=bool(ids) and all(name in ranges for name in ids),
                parameter_ids=list(ids),
                missing_parameter_ids=[name for name in ids if name not in ranges],
            )
            for key, ids in ACTION_PARAMETERS.items()
        ],
    )


def preset(
    clip_id: str, source_id: str, start: int, duration: int
) -> dict[str, object]:
    return {
        "clipId": clip_id,
        "trackType": "PRESET",
        "sourceId": source_id,
        "startMs": start,
        "durationMs": duration,
    }


def request_data(clips: list[dict[str, object]]) -> dict[str, object]:
    return {
        "taskId": "test-motion",
        "modelVersionId": "421",
        "modelManifestRef": "saved/model.model3.json",
        "durationMs": 2000,
        "clips": clips,
    }


def sample(segments: list[float], time: float) -> float:
    first_time, first_value = segments[:2]
    for offset in range(2, len(segments), 3):
        kind, next_time, next_value = segments[offset : offset + 3]
        assert kind == 0
        if first_time <= time <= next_time:
            return first_value + (next_value - first_value) * (time - first_time) / (
                next_time - first_time
            )
        first_time, first_value = next_time, next_value
    raise AssertionError("曲线没有覆盖所需时间点")


def test_combines_independent_parameters_and_aligns_audio(
    tmp_path: Path, model: Capabilities
) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("缺少 FFmpeg")
    # 前 200ms 是片段裁切区。其余样本用正负振幅验证 RMS 能驱动开合。
    with wave.open(str(tmp_path / "voice.wav"), "wb") as audio:
        audio.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        audio.writeframes(
            struct.pack("<hh", 0, 0) * 1600 + struct.pack("<hh", 2000, -2000) * 3200
        )
    request = MotionRequest.model_validate(
        request_data(
            [
                preset("face", "smile", 0, 1700),
                preset("head", "nod", 400, 800),
                {
                    "clipId": "voice",
                    "trackType": "SPEECH_AUDIO",
                    "sourceId": "dialogue",
                    "sourceRef": "voice.wav",
                    "sourceStartMs": 200,
                    "startMs": 550,
                    "durationMs": 650,
                },
            ]
        )
    )
    motion = compile_motion(
        request, model, Settings(_env_file=None, live2d_input_root=tmp_path)
    )
    curves = {curve["Id"]: curve["Segments"] for curve in motion["Curves"]}
    assert sample(curves["ParamMouthOpenY"], 0.549) == 0
    assert sample(curves["ParamMouthOpenY"], 0.680) > 0
    assert sample(curves["ParamMouthForm"], 0.680) > 0
    assert sample(curves["ParamAngleY"], 0.680) < 0
    assert all(sample(curve, 2.0) == 0 for curve in curves.values())
    assert sample(curves["ParamMouthOpenY"], 0.951) == 0


def test_conflict_reports_both_clips_interval_and_missing_hand(
    model: Capabilities,
) -> None:
    request = MotionRequest.model_validate(
        request_data(
            [
                preset("one", "smile", 0, 1000),
                preset("two", "smile", 700, 600),
                preset("hand", "right-hand-shape", 300, 600),
            ]
        )
    )
    with pytest.raises(MotionError) as error:
        compile_motion(request, model, Settings(_env_file=None))
    items = {item.code: item for item in error.value.diagnostics}
    conflict = items["PARAMETER_CONFLICT"]
    assert conflict.parameter_ids == ["ParamMouthForm"]
    assert conflict.clip_ids == ["one", "two"]
    assert (conflict.start_ms, conflict.end_ms) == (700, 1000)
    assert items["CAPABILITY_MISSING"].capability_id == "right-hand-shape"


def test_motion_task_idempotency_publishes_reusable_immutable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: Capabilities
) -> None:
    monkeypatch.setenv("LIVE2D_OUTPUT_ROOT", str(tmp_path))
    module = importlib.import_module("app.pipeline.live2d_motion_tasks")
    monkeypatch.setattr(module, "read_capabilities", lambda *_: model)

    def wait(client: TestClient, task_id: str) -> dict:
        deadline = monotonic() + 3
        while monotonic() < deadline:
            response = client.get(f"{ROUTE}/{task_id}")
            assert response.status_code == 200
            state = response.json()
            if state["status"] in {"SUCCEEDED", "FAILED"}:
                return state
            sleep(0.01)
        pytest.fail("组合动作超时")

    req = request_data(
        [preset("one", "smile", 0, 1000), preset("two", "smile", 1000, 1000)]
    )
    with TestClient(app) as client:
        accepted = client.post(ROUTE, json=req)
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "QUEUED"
        state = wait(client, "test-motion")
        assert state["status"] == "SUCCEEDED", state
        result = state["result"]
        artifact = tmp_path / result["artifactRef"]
        assert (
            hashlib.sha256(artifact.read_bytes()).hexdigest()
            == result["artifactSha256"]
        )
        assert json.loads(artifact.read_bytes())["Version"] == 3
        assert client.post(ROUTE, json=req).json() == state
        assert client.post(ROUTE, json={**req, "durationMs": 2100}).status_code == 409

        saved = {
            "clipId": "saved",
            "trackType": "SAVED_ACTION",
            "sourceId": "act-17",
            "sourceRef": result["artifactRef"],
            "sourceSha256": result["artifactSha256"],
            "startMs": 200,
            "durationMs": 1500,
        }
        assert (
            client.post(
                ROUTE, json={**req, "taskId": "reuse", "clips": [saved]}
            ).status_code
            == 202
        )
        assert wait(client, "reuse")["status"] == "SUCCEEDED"
        saved["sourceSha256"] = "0" * 64
        assert (
            client.post(
                ROUTE, json={**req, "taskId": "invalid", "clips": [saved]}
            ).status_code
            == 202
        )
        failed = wait(client, "invalid")
        assert failed["status"] == "FAILED" and failed["result"] is None
        assert failed["diagnostics"][0]["code"] == "MOTION_SOURCE_VERSION_MISMATCH"


def test_java_bridge_compiles_overlapping_independent_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: Capabilities
) -> None:
    monkeypatch.setenv("LIVE2D_OUTPUT_ROOT", str(tmp_path))
    module = importlib.import_module("app.pipeline.live2d_motion_tasks")
    monkeypatch.setattr(module, "read_capabilities", lambda *_: model)
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/live2d/combo-motions/compile",
            json={
                "requestId": "d" * 32,
                "projectId": 12,
                "modelVersionId": "421",
                "modelManifestRef": "saved/model.model3.json",
                "name": "微笑点头",
                "durationMs": 1500,
                "segments": [
                    {
                        "segmentId": preset_id,
                        "trackType": "MOTION",
                        "sourceId": f"preset:{preset_id}",
                        "sourceVersionId": "1",
                        "presetId": preset_id,
                        "startTimeMs": 100,
                        "durationMs": 1000,
                    }
                    for preset_id in ("smile", "nod")
                ],
            },
        )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "READY" and result["missingCapabilities"] == []
    manifest = result["playbackManifest"]
    assert manifest["compileMode"] == "PARAMETER_CURVES"
    motion = json.loads(
        (tmp_path / manifest["compiledMotion"]["artifactRef"]).read_bytes()
    )
    assert {curve["Id"] for curve in motion["Curves"]} == {
        "ParamMouthForm",
        "ParamAngleY",
    }
