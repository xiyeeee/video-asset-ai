import logging
import math

import torch
from PIL import Image
from torchvision.ops import nms
from transformers import (
    GroundingDinoForObjectDetection,
    GroundingDinoProcessor,
    Sam2Model,
    Sam2Processor,
)

from app.characters.extract import Detection
from app.core.config import Settings

logger = logging.getLogger(__name__)

MODEL_REVISIONS = {
    "IDEA-Research/grounding-dino-tiny": "a2bb814dd30d776dcf7e30523b00659f4f141c71",
    "facebook/sam2.1-hiera-tiny": "de431c4043854a71d8101e17995dfe596bf101a5",
}


class GroundedSam2:
    def __init__(self, settings: Settings, box_threshold: float) -> None:
        device = settings.character_device
        if device not in {"cpu", "mps"}:
            raise ValueError(f"未知 CHARACTER_DEVICE {device}，支持 cpu 和 mps")
        if device == "mps" and not torch.backends.mps.is_available():
            raise ValueError(
                "当前环境不能运行 PyTorch MPS，请设置 CHARACTER_DEVICE=cpu"
            )
        self.device = torch.device(device)
        self.box_threshold = box_threshold

        detector_id = settings.grounding_dino_model
        logger.info("加载 Grounding DINO %s，设备 %s", detector_id, self.device)
        self.detector_processor = GroundingDinoProcessor.from_pretrained(
            detector_id,
            revision=MODEL_REVISIONS.get(detector_id),
        )
        self.detector = (
            GroundingDinoForObjectDetection.from_pretrained(
                detector_id,
                revision=MODEL_REVISIONS.get(detector_id),
                disable_custom_kernels=True,
                use_safetensors=True,
            )
            .to(self.device)
            .eval()
        )

        sam_id = settings.sam2_model
        logger.info("加载 SAM 2 %s，设备 %s", sam_id, self.device)
        self.sam_processor = Sam2Processor.from_pretrained(
            sam_id, revision=MODEL_REVISIONS.get(sam_id)
        )
        self.sam = (
            Sam2Model.from_pretrained(
                sam_id,
                revision=MODEL_REVISIONS.get(sam_id),
                use_safetensors=True,
            )
            .to(self.device)
            .eval()
        )

    @torch.inference_mode()
    def detect(self, image: Image.Image) -> list[Detection]:
        inputs = self.detector_processor(
            images=image, text="person.", return_tensors="pt"
        ).to(self.device)
        outputs = self.detector(**inputs)
        width, height = image.size
        result = self.detector_processor.post_process_grounded_object_detection(
            outputs,
            input_ids=inputs["input_ids"],
            threshold=self.box_threshold,
            target_sizes=[(height, width)],
        )[0]
        boxes, scores = result["boxes"].cpu(), result["scores"].cpu()
        # 使用较高的 IoU 阈值去掉同一人物的近似框，保留有交叠但位置不同的人物。
        boxes[:, 0::2].clamp_(0, width)
        boxes[:, 1::2].clamp_(0, height)
        valid = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
        boxes, scores = boxes[valid], scores[valid]
        kept = nms(boxes, scores, iou_threshold=0.7)
        detections = []
        for box, score in zip(boxes[kept].tolist(), scores[kept].tolist(), strict=True):
            left, top, right, bottom = box
            detections.append(
                Detection(
                    box=(
                        math.floor(left),
                        math.floor(top),
                        math.ceil(right),
                        math.ceil(bottom),
                    ),
                    score=score,
                )
            )
        return sorted(detections, key=lambda detection: detection.box)

    @torch.inference_mode()
    def segment(
        self, image: Image.Image, detections: list[Detection]
    ) -> list[Image.Image]:
        inputs = self.sam_processor(
            images=image,
            input_boxes=[[list(detection.box) for detection in detections]],
            return_tensors="pt",
        )
        original_sizes = inputs.pop("original_sizes")
        outputs = self.sam(**inputs.to(self.device), multimask_output=False)
        # SAM 2 为每个框计算一份掩码。同一像素有多份前景预测时，保留分数最高的一份。
        masks = self.sam_processor.post_process_masks(
            outputs.pred_masks.cpu(),
            original_sizes,
            apply_non_overlapping_constraints=True,
        )[0][:, 0]
        return [Image.fromarray((mask.to(torch.uint8) * 255).numpy()) for mask in masks]
