from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.extract_characters import main

if TYPE_CHECKING:
    from PIL.Image import Image as PillowImage

image_module = pytest.importorskip("PIL.Image")
extraction = pytest.importorskip("app.characters.extract")


class TwoPeople:
    def detect(self, image: PillowImage) -> list[extraction.Detection]:
        return [
            extraction.Detection(box=(0, 0, 4, image.height), score=0.91),
            extraction.Detection(box=(4, 0, 8, image.height), score=0.86),
        ]

    def segment(
        self, image: PillowImage, detections: list[extraction.Detection]
    ) -> list[PillowImage]:
        masks = []
        for detection in detections:
            mask = image_module.new("L", image.size)
            mask.paste(255, detection.box)
            masks.append(mask)
        return masks


@pytest.fixture
def frames_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "frames.json"
    frames = [
        {"path": "frame-000001.jpg", "timestampMs": 0.0},
        {"path": "frame-000002.jpg", "timestampMs": 2002.125},
    ]
    for frame in frames:
        image_module.new("RGB", (8, 4), "red").save(tmp_path / frame["path"])
    manifest.write_text(json.dumps({"frames": frames}), encoding="utf-8")
    return manifest


def test_writes_separate_cutouts_with_source_timestamps(frames_manifest: Path) -> None:
    output = frames_manifest.parent / "characters"
    paths = ["frame-000002.jpg", "frame-000001.jpg"]
    frames = extraction.select_frames(frames_manifest, paths)
    extraction.extract_characters(frames_manifest, frames, output, TwoPeople())
    manifest = json.loads((output / "characters.json").read_text(encoding="utf-8"))

    assert (output / manifest["framesManifest"]).resolve() == frames_manifest
    assert [frame["sourceFrame"] for frame in manifest["frames"]] == paths
    assert [item["characterId"] for item in manifest["characters"]] == [1, 2, 1, 2]
    assert [item["timestampMs"] for item in manifest["characters"]] == [
        2002.125,
        2002.125,
        0,
        0,
    ]
    for item in manifest["characters"]:
        assert item["score"] in {0.91, 0.86}
        with image_module.open(output / item["debugImage"]) as debug:
            assert debug.format == "JPEG"
            assert debug.size == (8, 4)
        with image_module.open(output / item["pngPath"]) as cutout:
            assert cutout.mode == "RGBA"
            alpha = cutout.getchannel("A")
            assert alpha.getbbox() == tuple(item["box"])
            assert alpha.getextrema() == (0, 255)


def test_cli_rejects_unknown_frame_before_loading_models(
    frames_manifest: Path, caplog: pytest.LogCaptureFixture
) -> None:
    output = frames_manifest.parent / "characters"
    assert (
        main(
            [
                "--frames",
                str(frames_manifest),
                "--frame",
                "unknown.jpg",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert "截图未出现在清单中" in caplog.text
    assert not output.exists()


def test_rejects_frame_outside_manifest_directory(tmp_path: Path) -> None:
    folder = tmp_path / "source"
    folder.mkdir()
    (tmp_path / "outside.jpg").touch()
    (folder / "frame.jpg").symlink_to(tmp_path / "outside.jpg")
    manifest = folder / "frames.json"
    manifest.write_text(
        '{"frames":[{"path":"frame.jpg","timestampMs":0}]}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="截图路径须位于清单目录内"):
        extraction.select_frames(manifest, ["frame.jpg"])


def test_segmentation_failure_leaves_no_partial_results(frames_manifest: Path) -> None:
    class EmptyMask(TwoPeople):
        def segment(
            self, image: PillowImage, detections: list[extraction.Detection]
        ) -> list[PillowImage]:
            masks = super().segment(image, detections)
            masks[-1] = image_module.new("L", image.size)
            return masks

    output = frames_manifest.parent / "characters"
    frames = extraction.select_frames(frames_manifest, ["frame-000001.jpg"])
    with pytest.raises(ValueError, match="人物 #2 的掩码无前景"):
        extraction.extract_characters(frames_manifest, frames, output, EmptyMask())
    assert not output.exists()
