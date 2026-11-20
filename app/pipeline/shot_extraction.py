from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.pipeline.asset_task_models import ShotResult, ShotStage
from app.storage.shared_files import (
    checked_file,
    publish_task_directory,
    stage_task_directory,
)
from app.video.shots import detect_shots, extract_shot_previews


def extract_shots(
    task_id: str,
    source: Path,
    output_root: Path,
    settings: Settings,
    report: Callable[[ShotStage], None],
) -> ShotResult:
    report(ShotStage.DETECTING_SHOTS)
    analysis = detect_shots(source, settings)
    manifest = analysis.manifest
    with stage_task_directory(output_root) as temporary:
        staged = Path(temporary) / "result"
        staged.mkdir()
        report(ShotStage.EXTRACTING_PREVIEWS)
        extract_shot_previews(source, staged, analysis.previews, settings)
        report(ShotStage.WRITING_RESULTS)
        for shot in manifest.shots:
            checked_file(staged, staged, shot.preview_path)
        (staged / "shots.json").write_text(
            manifest.model_dump_json(indent=2, by_alias=True) + "\n", encoding="utf-8"
        )
        destination = output_root / f"{task_id}-shots-{uuid4().hex}"
        publish_task_directory(output_root, staged, destination)
    return ShotResult(
        manifest_ref=(destination / "shots.json").relative_to(output_root).as_posix(),
        shot_count=len(manifest.shots),
    )
