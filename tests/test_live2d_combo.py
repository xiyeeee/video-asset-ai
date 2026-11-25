from fastapi.testclient import TestClient

from app.main import app
from app.pipeline.live2d_combo import ComboCompileRequest, compile_combo_motion


def _request(segments: list[dict[str, object]]) -> ComboCompileRequest:
    return ComboCompileRequest.model_validate(
        {
            "requestId": "a" * 32,
            "projectId": 12,
            "modelVersionId": "421",
            "name": "开场动作",
            "durationMs": 3000,
            "segments": segments,
        }
    )


def _segment(
    segment_id: str, track_type: str, start: int, duration: int
) -> dict[str, object]:
    return {
        "segmentId": segment_id,
        "trackType": track_type,
        "sourceId": f"source-{segment_id}",
        "sourceVersionId": "421" if track_type == "MOTION" else "997",
        "startTimeMs": start,
        "durationMs": duration,
        "sourceStartMs": None if track_type == "MOTION" else 500,
        "sourceDurationMs": 2000,
    }


def test_compile_returns_canonical_timeline_for_non_overlapping_tracks() -> None:
    result = compile_combo_motion(
        _request(
            [
                _segment("b" * 32, "DIALOGUE", 100, 1200),
                _segment("a" * 32, "MOTION", 0, 1000),
            ]
        )
    )

    assert result.status == "READY"
    assert result.playback_manifest["modelVersionId"] == "421"
    segments = result.playback_manifest["segments"]
    assert [segment["segmentId"] for segment in segments] == ["a" * 32, "b" * 32]


def test_compile_reports_motion_conflicts_and_duration_errors() -> None:
    result = compile_combo_motion(
        _request(
            [
                _segment("a" * 32, "MOTION", 0, 1500),
                _segment("b" * 32, "MOTION", 1000, 1000),
                _segment("c" * 32, "DIALOGUE", 2500, 1000),
            ]
        )
    )

    assert result.status == "CONFLICT"
    assert {issue.code for issue in result.issues} == {
        "MOTION_OVERLAP",
        "SEGMENT_OUT_OF_RANGE",
    }


def test_internal_compile_endpoint_returns_playback_manifest() -> None:
    request = {
        "requestId": "c" * 32,
        "projectId": 12,
        "modelVersionId": "421",
        "name": "开场动作",
        "durationMs": 3000,
        "segments": [
            {**_segment("d" * 32, "MOTION", 0, 1000), "sourceDurationMs": None}
        ],
    }
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/live2d/combo-motions/compile", json=request
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY"
    assert body["playbackManifest"]["modelVersionId"] == "421"
    assert body["missingCapabilities"] == ["MOTION_DURATION_METADATA"]
