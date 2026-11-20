from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

    from app.characters.extract import Detection


DetectPeople = Callable[["Image"], list["Detection"]]


class DetectionStatus(StrEnum):
    DETECTED = "DETECTED"
    NOT_DETECTED = "NOT_DETECTED"
    UNKNOWN = "UNKNOWN"


class ReviewStatus(StrEnum):
    CLEAN_CANDIDATE = "CLEAN_CANDIDATE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


def review_status(people: DetectionStatus, subtitles: DetectionStatus) -> ReviewStatus:
    if (
        people is DetectionStatus.NOT_DETECTED
        and subtitles is DetectionStatus.NOT_DETECTED
    ):
        return ReviewStatus.CLEAN_CANDIDATE
    return ReviewStatus.NEEDS_REVIEW


def score_candidate(
    path: Path, detect_people: DetectPeople
) -> tuple[DetectionStatus, float, float]:
    from PIL import Image, ImageDraw, ImageFilter, ImageStat

    with Image.open(path) as source:
        rgb = source.convert("RGB")
    detections = detect_people(rgb)
    width, height = rgb.size
    mask = Image.new("L", rgb.size)
    draw = ImageDraw.Draw(mask)
    # 人物框面积取并集，避免重叠的框把同一块画面计算多次。
    for detection in detections:
        left, top, right, bottom = detection.box
        draw.rectangle((left, top, right - 1, bottom - 1), fill=255)
    covered = mask.histogram()[255] / (width * height)

    gray = rgb.convert("L")
    gray.thumbnail((320, 320))
    edges = gray.filter(ImageFilter.FIND_EDGES)
    if edges.width > 2 and edges.height > 2:
        edges = edges.crop((1, 1, edges.width - 1, edges.height - 1))
    sharpness = ImageStat.Stat(edges).mean[0]
    status = DetectionStatus.DETECTED if detections else DetectionStatus.NOT_DETECTED
    return status, covered, sharpness
