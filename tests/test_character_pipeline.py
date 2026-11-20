from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.core.config import Settings
from app.pipeline.character_extraction import CharacterExtraction
from app.pipeline.character_task_models import TaskProgress, TaskRequest, TaskStage

if TYPE_CHECKING:
    from PIL.Image import Image

    from app.characters.extract import Detection


class NoDetections:
    def detect(self, image: Image) -> list[Detection]:
        return []

    def segment(self, image: Image, detections: list[Detection]) -> list[Image]:
        pytest.fail("没有人物框时无需执行分割")


@pytest.mark.parametrize("debug_image_missing", [False, True])
def test_publishes_empty_result_only_after_checking_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug_image_missing: bool
) -> None:
    pytest.importorskip("PIL.Image")
    if not (ffmpeg := shutil.which("ffmpeg")) or not (
        ffprobe := shutil.which("ffprobe")
    ):
        pytest.skip("集成测试需要系统 FFmpeg 和 FFprobe")
    output_root = tmp_path / "output"
    source = tmp_path / "empty.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=black:size=32x32:rate=4:duration=1",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
        timeout=10,
    )

    extraction = CharacterExtraction(
        Settings(
            character_output_root=output_root, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe
        )
    )
    monkeypatch.setitem(vars(extraction), "person_models", NoDetections())
    if debug_image_missing:

        def skip_debug_image(
            image: Image, detections: list[Detection], path: Path
        ) -> None:
            pass

        monkeypatch.setattr(
            "app.characters.extract.write_debug_image", skip_debug_image
        )

    task = TaskRequest(
        taskId="empty", sourceVideoRef=source.name, frameIntervalSeconds=2
    )
    stages: list[TaskStage] = []

    def report(stage: TaskStage, progress: TaskProgress) -> None:
        stages.append(stage)
        assert not list(output_root.glob("empty-*"))

    if debug_image_missing:
        with pytest.raises(ValueError, match="结果清单引用了无效文件"):
            extraction.run(task, source, report)
        assert not list(output_root.iterdir())
    else:
        result = extraction.run(task, source, report)
        assert result.character_asset_count == 0
        path = output_root / result.manifest_ref
        manifest = json.loads(path.read_text())
        assert manifest["characters"] == []
        assert (path.parent / manifest["frames"][0]["debugImage"]).is_file()

    assert stages == list(TaskStage)
