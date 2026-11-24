"""用现有 Grounding DINO 和 SAM 2 拆出一张角色图的可见部件。"""

from __future__ import annotations

import argparse
import logging
import math
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from app.characters.extract import Detection
from app.core.config import Settings

if TYPE_CHECKING:
    from PIL.Image import Image

    from app.characters.models import GroundedSam2

logger = logging.getLogger(__name__)
PARTS_PROMPT = "hair. face. eye. mouth. eyebrow. nose."


def detect_parts(image: Image, models: GroundedSam2) -> dict[str, list[Detection]]:
    import torch

    inputs = models.detector_processor(
        images=image, text=PARTS_PROMPT, return_tensors="pt"
    ).to(models.device)
    with torch.inference_mode():
        output = models.detector(**inputs)
    result = models.detector_processor.post_process_grounded_object_detection(
        output,
        input_ids=inputs["input_ids"],
        threshold=0.22,
        text_threshold=0.20,
        target_sizes=[(image.height, image.width)],
    )[0]
    parts: dict[str, list[Detection]] = {}
    for label, box, score in zip(
        result["text_labels"],
        result["boxes"].tolist(),
        result["scores"].tolist(),
        strict=True,
    ):
        left, top, right, bottom = box
        bounds = (
            max(0, math.floor(left)),
            max(0, math.floor(top)),
            min(image.width, math.ceil(right)),
            min(image.height, math.ceil(bottom)),
        )
        if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
            parts.setdefault(label, []).append(Detection(box=bounds, score=score))
    return parts


def split_image(source: Path, output: Path, models: GroundedSam2) -> None:
    from PIL import Image as PILImage
    from PIL import ImageChops, ImageOps

    with PILImage.open(source) as raw:
        original = ImageOps.exif_transpose(raw).convert("RGBA")
    alpha = original.getchannel("A")
    gray = PILImage.new("RGBA", original.size, (128, 128, 128, 255))
    image = PILImage.alpha_composite(gray, original).convert("RGB")
    if alpha.getextrema() == (255, 255):
        people = models.detect(image)
        if not people:
            raise ValueError("未找到可拆层的主体人物")
        alpha = models.segment(image, [max(people, key=lambda match: match.score)])[0]

    parts = detect_parts(image, models)
    if not parts.get("face") or not parts.get("hair"):
        raise ValueError("基础拆层需检测到脸和头发")
    face, hair = (
        max(parts[name], key=lambda match: match.score) for name in ("face", "hair")
    )

    def mask_of(part: Detection, area: Image) -> Image:
        # 独立做提示框分割，再限制在该部件可见范围，保留与其他部件重叠的像素。
        left, top, right, bottom = part.box
        x_margin = max(2, (right - left) // 12)
        y_margin = max(2, (bottom - top) // 12)
        bounds = (
            max(0, left - x_margin),
            max(0, top - y_margin),
            min(image.width, right + x_margin),
            min(image.height, bottom + y_margin),
        )
        segmented = models.segment(image, [part])[0]
        limited = PILImage.new("L", image.size)
        limited.paste(segmented.crop(bounds), bounds[:2])
        return ImageChops.multiply(limited, area)

    hair_mask = mask_of(hair, alpha)
    face_mask = ImageChops.subtract(mask_of(face, alpha), hair_mask)
    if not hair_mask.getbbox() or not face_mask.getbbox():
        raise ValueError("脸或头发的分割结果为空")
    torso_mask = ImageChops.subtract(alpha, ImageChops.lighter(face_mask, hair_mask))
    if not torso_mask.getbbox():
        raise ValueError("躯干部件的分割结果为空")

    layers = {
        "00_torso": torso_mask,
        "10_face_base": face_mask,
        "90_hair_front": hair_mask,
    }
    center_x = (face.box[0] + face.box[2]) / 2
    for label, prefix in (("eye", "20"), ("eyebrow", "25")):
        sided: dict[str, Detection] = {}
        for part in sorted(parts.get(label, []), key=lambda match: -match.score):
            mid_x = (part.box[0] + part.box[2]) / 2
            if face.box[0] <= mid_x <= face.box[2]:
                side = "r" if mid_x < center_x else "l"
                sided.setdefault(side, part)
        for side, detection in sided.items():
            layers[f"{prefix}_{label}_{side}"] = mask_of(detection, face_mask)
    for label, prefix in (("nose", "30"), ("mouth", "35")):
        if parts.get(label):
            detection = max(parts[label], key=lambda match: match.score)
            layers[f"{prefix}_{label}"] = mask_of(detection, face_mask)

    output.mkdir(parents=True)
    for name, mask in layers.items():
        if mask.getbbox():
            layer = original.copy()
            layer.putalpha(mask)
            layer.save(output / f"{name}.png")
    logger.info("基础分层导出 %d 个可见部件", sum(1 for _ in output.glob("*.png")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from app.characters.models import GroundedSam2

    started_at = perf_counter()
    try:
        models = GroundedSam2(Settings(), 0.30)
        split_image(args.image, args.output, models)
    except (OSError, RuntimeError, ValueError):
        logger.exception("基础角色拆层失败")
        return 1
    logger.info("基础拆层完成，耗时 %.2f 秒", perf_counter() - started_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
