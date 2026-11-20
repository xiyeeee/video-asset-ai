import logging
import math
from collections.abc import Callable
from os.path import relpath
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Protocol

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from app.video.ffmpeg import check_output_directory

logger = logging.getLogger(__name__)


class SourceFrame(BaseModel):
    path: str
    timestamp_ms: float = Field(alias="timestampMs", ge=0, allow_inf_nan=False)


class FramesManifest(BaseModel):
    frames: list[SourceFrame]


class Detection(BaseModel):
    box: tuple[int, int, int, int]
    score: float


class ProcessedFrame(BaseModel):
    source_frame: str = Field(serialization_alias="sourceFrame")
    timestamp_ms: int | float = Field(serialization_alias="timestampMs")
    debug_image: str = Field(serialization_alias="debugImage")


class Character(ProcessedFrame, Detection):
    character_id: int = Field(serialization_alias="characterId")
    png_path: str = Field(serialization_alias="pngPath")


class CharactersManifest(BaseModel):
    frames_manifest: str = Field(serialization_alias="framesManifest")
    frames: list[ProcessedFrame]
    characters: list[Character]


class PersonModels(Protocol):
    def detect(self, image: Image.Image) -> list[Detection]: ...

    def segment(
        self, image: Image.Image, detections: list[Detection]
    ) -> list[Image.Image]: ...


def select_frames(manifest: Path, frame_paths: list[str]) -> list[SourceFrame]:
    frames = FramesManifest.model_validate_json(manifest.read_bytes()).frames
    by_path = {frame.path: frame for frame in frames}
    if len(by_path) != len(frames):
        raise ValueError(f"截图清单包含重复路径 {manifest}")
    if not frame_paths or len(set(frame_paths)) != len(frame_paths):
        raise ValueError("请指定一张或多张截图，每个 --frame 路径只能选一次")

    selected = []
    parent = manifest.absolute().parent.resolve()
    for frame_path in frame_paths:
        if frame_path not in by_path:
            raise ValueError(f"截图未出现在清单中 {frame_path}")
        path = Path(frame_path)
        source = parent / path
        if (
            path.is_absolute()
            or ".." in path.parts
            or not source.resolve().is_relative_to(parent)
        ):
            raise ValueError(f"截图路径须位于清单目录内 {frame_path}")
        if not source.is_file():
            raise ValueError(f"截图文件不存在 {source}")
        selected.append(by_path[frame_path])
    return selected


def write_debug_image(
    image: Image.Image, detections: list[Detection], path: Path
) -> None:
    preview = image.copy()
    draw = ImageDraw.Draw(preview)
    line_width = max(2, image.width // 400)
    font_size = max(12, image.width // 85)
    for index, detection in enumerate(detections, start=1):
        left, top, right, bottom = detection.box
        color = ("#26e5ff", "#ffe066", "#f783ff")[(index - 1) % 3]
        label = f"#{index} {detection.score:.3f}"
        position = (left + 2, max(0, top - font_size - 6))
        draw.rectangle(
            (left, top, right - 1, bottom - 1), outline=color, width=line_width
        )
        draw.rectangle(
            draw.textbbox(position, label, font_size=font_size), fill="#101010"
        )
        draw.text(position, label, fill=color, font_size=font_size)
    preview.save(path)


def detect_in_frame(
    manifest: Path, frame: SourceFrame, model: PersonModels
) -> list[Detection]:
    try:
        with Image.open(manifest.parent / frame.path) as source:
            return model.detect(source.convert("RGB"))
    except Exception as exc:
        raise RuntimeError(f"检测截图 {frame.path} 失败，{exc}") from exc


def extract_characters(
    manifest: Path,
    frames: list[SourceFrame],
    output: Path,
    model: PersonModels,
    *,
    on_detected: Callable[[int], None] | None = None,
    on_writing: Callable[[], None] | None = None,
    whole_milliseconds: bool = False,
) -> CharactersManifest:
    started_at = perf_counter()
    output = output.absolute()
    manifest = manifest.absolute()
    check_output_directory(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = CharactersManifest(
        frames_manifest=Path(relpath(manifest, output)).as_posix(),
        frames=[],
        characters=[],
    )
    logger.info("开始人物提取，%d 张截图，输出 %s", len(frames), output)
    detections_by_frame = [detect_in_frame(manifest, frame, model) for frame in frames]
    if on_detected is not None:
        on_detected(sum(len(detections) for detections in detections_by_frame))
    with TemporaryDirectory(prefix=".characters-", dir=output.parent) as temporary:
        staged = Path(temporary) / "result"
        staged.mkdir()
        for frame_index, (frame, detections) in enumerate(
            zip(frames, detections_by_frame, strict=True), start=1
        ):
            try:
                with Image.open(manifest.parent / frame.path) as source:
                    image = source.convert("RGB")
                masks = model.segment(image, detections) if detections else []
                if len(masks) != len(detections):
                    raise ValueError(
                        f"收到 {len(masks)} 个掩码，检测框数为 {len(detections)}"
                    )

                # 每张截图单独编号，输出名不依赖截图 basename，允许不同目录下有同名帧。
                frame_dir = f"frame-{frame_index:06d}"
                (staged / frame_dir).mkdir()
                debug_path = f"{frame_dir}/detections.jpg"
                write_debug_image(image, detections, staged / debug_path)
                # Java 以整数毫秒消费实际 PTS，CLI 继续保留抽帧清单中的小数精度。
                timestamp_ms = (
                    math.floor(frame.timestamp_ms + 0.5)
                    if whole_milliseconds
                    else frame.timestamp_ms
                )
                processed = ProcessedFrame(
                    source_frame=frame.path,
                    timestamp_ms=timestamp_ms,
                    debug_image=debug_path,
                )
                for index, (detection, mask) in enumerate(
                    zip(detections, masks, strict=True), start=1
                ):
                    if (
                        mask.mode != "L"
                        or mask.size != image.size
                        or mask.getextrema() != (0, 255)
                    ):
                        raise ValueError(
                            f"人物 #{index} 的掩码无前景、无透明背景或尺寸不符"
                        )
                    cutout = Image.new("RGBA", image.size)
                    cutout.paste(image, mask=mask)
                    png_path = f"{frame_dir}/person-{index:03d}.png"
                    cutout.save(staged / png_path)
                    result.characters.append(
                        Character(
                            **processed.model_dump(),
                            **detection.model_dump(),
                            character_id=index,
                            png_path=png_path,
                        )
                    )
                result.frames.append(processed)
                logger.info(
                    "截图 %s（%g 毫秒）检测到 %d 人",
                    frame.path,
                    frame.timestamp_ms,
                    len(detections),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(f"处理截图 {frame.path} 失败，{exc}") from exc
        if on_writing is not None:
            on_writing()
        (staged / "characters.json").write_text(
            result.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8"
        )
        check_output_directory(output)
        staged.rename(output)
    logger.info(
        "人物提取完成，%d 张截图、%d 个 PNG，耗时 %.2f 秒",
        len(frames),
        len(result.characters),
        perf_counter() - started_at,
    )
    return result
