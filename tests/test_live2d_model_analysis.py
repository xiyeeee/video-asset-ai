import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

ANALYZE = "/internal/v1/live2d/model-capabilities/analyze"
RESOLVE = "/internal/v1/live2d/motion-templates/resolve"


def write(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def model_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIVE2D_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("LIVE2D_WEB_CORE_JS", str(tmp_path / "not-installed.js"))
    write(
        tmp_path / "model.model3.json",
        {
            "Version": 3,
            "FileReferences": {
                "Moc": "model.moc3",
                "DisplayInfo": "model.cdi3.json",
                "Expressions": [{"Name": "blush", "File": "blush.exp3.json"}],
            },
        },
    )
    (tmp_path / "model.moc3").write_bytes(b"fixture")
    write(
        tmp_path / "model.cdi3.json",
        {
            "Parameters": [
                {"Id": "ParamCustomGaze", "Name": "Horizontal Gaze"},
                {"Id": "ParamMouthOpenY", "Name": "Mouth Open"},
            ],
            "Parts": [{"Id": "right_hand", "Name": "Right Hand"}],
        },
    )
    write(
        tmp_path / "blush.exp3.json",
        {
            "Type": "Live2D Expression",
            "Parameters": [{"Id": "ParamCheek", "Value": 1, "Blend": "Add"}],
        },
    )
    return tmp_path


def semantics(response: dict) -> dict[str, dict]:
    return {
        item["id"]: item
        for group in response["capabilities"]
        for item in group["capabilities"]
    }


def test_incomplete_inventory_preserves_metadata_and_never_confirms_name_match(
    model_root: Path,
) -> None:
    request = {"modelVersionId": "trial", "modelManifestRef": "model.model3.json"}
    with TestClient(app) as client:
        response = client.post(ANALYZE, json=request)
        assert response.status_code == 200
        invalid = client.post(
            ANALYZE, json={**request, "modelManifestRef": "../outside.model3.json"}
        )
        assert invalid.status_code == 422

    result = response.json()
    groups = semantics(result)
    assert len(result["capabilities"]) == 9 and not result["parameterInventoryComplete"]
    assert groups["gaze-horizontal"]["status"] == "CANDIDATE"
    assert groups["gaze-horizontal"]["parameters"][0]["min"] is None
    assert groups["mouth-purse"]["status"] == "UNKNOWN"
    assert groups["hand-shape"]["partIds"] == ["right_hand"]
    assert groups["hand-shape"]["status"] == "UNKNOWN"
    assert groups["blush"]["assets"][0]["artifactRef"] == "blush.exp3.json"
    assert (
        result["modelMocSha256"]
        == hashlib.sha256((model_root / "model.moc3").read_bytes()).hexdigest()
    )
    assert any(
        item["code"] == "PARAMETER_INVENTORY_INCOMPLETE"
        for item in result["diagnostics"]
    )


def test_confirmed_mapping_resolves_asymmetric_ranges_and_reverse_direction(
    model_root: Path,
) -> None:
    request = {
        "modelVersionId": "trial",
        "modelManifestRef": "model.model3.json",
        "runtimeParameterRef": "runtime.json",
        "bindingVersionId": "custom",
        "templateId": "side-glance",
        "durationMs": 800,
        "tracks": [
            {
                "semanticId": "gaze-horizontal",
                "durationMs": 700,
                "startValue": -0.5,
                "endValue": 1,
                "amplitude": 0.5,
                "easing": "EASE_OUT",
            }
        ],
    }
    digest = hashlib.sha256((model_root / "model.moc3").read_bytes()).hexdigest()
    write(
        model_root / "runtime.json",
        {
            "mocVersion": 1,
            "mocSha256": digest,
            "parameters": [
                {
                    "id": "ParamCustomGaze",
                    "min": -3,
                    "max": 4,
                    "defaultValue": 1,
                    "active": True,
                    "activeIncreasing": True,
                    "activeDecreasing": True,
                }
            ],
        },
    )
    with TestClient(app) as client:
        missing = client.post(RESOLVE, json=request).json()
        assert missing["status"] == "UNRESOLVED" and missing["tracks"] == []
        assert any(
            item["code"] == "BINDING_UNCONFIRMED" for item in missing["diagnostics"]
        )
        for direction, expected in ((1, (0.0, 2.5)), (-1, (1.75, -1.0))):
            binding = {
                "semanticId": "gaze-horizontal",
                "parameterId": "ParamCustomGaze",
                "direction": direction,
            }
            response = client.post(
                RESOLVE,
                json={
                    **request,
                    "bindings": [binding],
                    "bindingVersionId": f"v{direction}",
                },
            )
            assert response.status_code == 200
            result = response.json()
            assert result["status"] == "READY"
            assert result["bindingVersionId"] == f"v{direction}"
            track = result["tracks"][0]
            assert (track["startValue"], track["endValue"]) == expected
            assert track["easing"] == "EASE_OUT" and track["durationMs"] == 700

        wrong = client.post(
            RESOLVE, json={**request, "expectedAssetFingerprint": "0" * 64}
        )
        assert wrong.status_code == 409
        assert wrong.json()["detail"]["code"] == "MODEL_VERSION_MISMATCH"


def test_confirmed_asset_binding_returns_verified_reference(model_root: Path) -> None:
    payload = {
        "modelVersionId": "trial",
        "modelManifestRef": "model.model3.json",
        "bindingVersionId": "reviewed",
        "templateId": "blush",
        "bindings": [{"semanticId": "blush", "assetRef": "blush.exp3.json"}],
        "durationMs": 1200,
        "tracks": [{"semanticId": "blush", "durationMs": 1200}],
    }
    with TestClient(app) as client:
        result = client.post(RESOLVE, json=payload)
    assert result.status_code == 200
    assert result.json()["status"] == "READY"
    asset = result.json()["tracks"][0]["asset"]
    assert asset["kind"] == "EXPRESSION"
    assert (
        asset["sha256"]
        == hashlib.sha256((model_root / "blush.exp3.json").read_bytes()).hexdigest()
    )
