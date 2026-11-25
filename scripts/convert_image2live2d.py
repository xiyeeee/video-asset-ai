"""通过独立安装的 image2live2d 绑定拆层素材并导出 Cubism 文件。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from image2live2d.backends.live2d.moc3_binary import Moc3
    from image2live2d.irr.schema import Rig

logger = logging.getLogger(__name__)


def preserve_canvas_aspect(
    moc: Moc3, width: int, height: int, position_index_unit: int
) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("拆层画布尺寸必须为正数")
    if width == height:
        return

    # 导出器把两个轴独立归一化到正方形。短轴按原图比例缩回，并同步调整画布。
    longest_side = max(width, height)
    scale_x, scale_y = width / longest_side, height / longest_side
    for field, factor in (
        ("width", scale_x),
        ("originX", scale_x),
        ("height", scale_y),
        ("originY", scale_y),
    ):
        moc.canvas[field] *= factor

    positions = moc.sections["keyformPositions"]["xys"]
    for geometry_name, keyform_name in (
        ("artMeshes", "artMeshKeyforms"),
        ("warpDeformers", "warpDeformerKeyforms"),
    ):
        geometry = moc.sections[geometry_name]
        keyform_positions = moc.sections[keyform_name][
            "keyformPositionSourcesBeginIndices"
        ]
        for index, vertex_count in enumerate(geometry["vertexCounts"]):
            # 网格变形器的子部件使用局部坐标。只缩放根网格和根变形器的模型空间顶点。
            if (
                geometry_name == "artMeshes"
                and geometry["parentDeformerIndices"][index] >= 0
            ):
                continue
            start = geometry["keyformSourcesBeginIndices"][index]
            count = geometry["keyformSourcesCounts"][index]
            for offset in keyform_positions[start : start + count]:
                first = offset // position_index_unit
                last = first + vertex_count
                positions[first:last] = [
                    (x * scale_x, y * scale_y) for x, y in positions[first:last]
                ]


def blend_mouth_layers(rig: Rig) -> None:
    lips = rig.mesh_for("35_mouth")
    cavity = rig.mesh_for("34_mouth_cavity")
    if lips is None or cavity is None:
        return
    parameters = {parameter.id: parameter for parameter in rig.parameters}
    opening = parameters.get("ParamMouthOpenY")
    if opening is not None and opening.max > opening.min:
        # 用开合参数在原嘴唇和张嘴层之间过渡，避免闭嘴纹理盖住牙齿。
        for keyform in opening.keyforms:
            weight = (keyform.value - opening.min) / (opening.max - opening.min)
            keyform.opacity_overrides.update(
                {"35_mouth": 1 - weight, "34_mouth_cavity": weight}
            )

    form = parameters.get("ParamMouthForm")
    if form is not None:
        # 张嘴层沿用同一横向位置的嘴角变形，让微笑和开合可以同时生效。
        nearest_lip_vertices = [
            min(
                range(len(lips.vertices)),
                key=lambda index: abs(lips.vertices[index][0] - x),
            )
            for x, _ in cavity.vertices
        ]
        for keyform in form.keyforms:
            if (lip_offsets := keyform.mesh_offsets.get("35_mouth")) is not None:
                keyform.mesh_offsets["34_mouth_cavity"] = [
                    lip_offsets[index] for index in nearest_lip_vertices
                ]


def export_rig(
    rig: Rig, layers: Path, output: Path, canvas_width: int, canvas_height: int
) -> None:
    from image2live2d.backends.live2d import cdi3, model3, physics3
    from image2live2d.backends.live2d.moc3_binary import write_moc3
    from image2live2d.backends.live2d.moc3_emit import (
        KEYFORM_POS_UNIT,
        build_atlas,
        rig_to_moc3,
    )

    output.mkdir(parents=True)
    (output / "textures").mkdir()
    atlas, texture_coordinates = build_atlas(rig, layers)
    atlas.save(output / "textures" / "atlas.png")
    moc = rig_to_moc3(rig, atlas_uv=texture_coordinates)
    preserve_canvas_aspect(moc, canvas_width, canvas_height, KEYFORM_POS_UNIT)
    (output / "model.moc3").write_bytes(write_moc3(moc))
    (output / "model.cdi3.json").write_text(
        json.dumps(cdi3.cdi3(rig), ensure_ascii=False), encoding="utf-8"
    )
    physics = None
    if rig.physics:
        physics = "model.physics3.json"
        (output / physics).write_text(
            json.dumps(physics3.physics3(rig), ensure_ascii=False), encoding="utf-8"
        )
    manifest = model3.model3(
        rig,
        moc="model.moc3",
        textures=["textures/atlas.png"],
        physics=physics,
        display_info="model.cdi3.json",
    )
    (output / "model.model3.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument(
        "--image", type=Path, help="使用本机拆层工具或已配置的 See-through 服务"
    )
    sources.add_argument("--layers", type=Path, help="从已有分层目录验证绑定与导出")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from image2live2d.core.decompose import from_layer_dir, from_service
    from image2live2d.pipeline import rig_from_stack

    try:
        with TemporaryDirectory(prefix="image2live2d-") as temp:
            started_at = perf_counter()
            if args.image is not None:
                service_url = os.environ.get("IMAGE2LIVE2D_DECOMPOSE_URL")
                splitter = os.environ.get("IMAGE2LIVE2D_LAYER_SPLITTER_ARGV")
                if bool(service_url) == bool(splitter):
                    raise ValueError(
                        "需配置一个拆层入口 IMAGE2LIVE2D_DECOMPOSE_URL 或 IMAGE2LIVE2D_LAYER_SPLITTER_ARGV"
                    )
                if service_url:
                    stack = from_service(
                        args.image,
                        service_url,
                        Path(temp),
                        token=os.environ.get("IMAGE2LIVE2D_DECOMPOSE_TOKEN"),
                    )
                else:
                    argv = json.loads(splitter)
                    if (
                        not isinstance(argv, list)
                        or not argv
                        or not all(isinstance(arg, str) and arg for arg in argv)
                    ):
                        raise ValueError(
                            "IMAGE2LIVE2D_LAYER_SPLITTER_ARGV 须为非空命令参数数组"
                        )
                    layers = Path(temp) / "local-layers"
                    subprocess.run(
                        [
                            *argv,
                            "--image",
                            str(args.image.resolve()),
                            "--output",
                            str(layers),
                        ],
                        check=True,
                        timeout=900,
                    )
                    stack = from_layer_dir(layers)
                logger.info("拆层完成，耗时 %.2f 秒", perf_counter() - started_at)
            else:
                stack = from_layer_dir(args.layers.resolve())
            layers = Path(stack.layers[0].texture_path).parent
            started_at = perf_counter()
            rig = rig_from_stack(
                stack, name=os.environ.get("LIVE2D_MODEL_NAME") or "model"
            )
            blend_mouth_layers(rig)
            logger.info("绑定完成，耗时 %.2f 秒", perf_counter() - started_at)
            started_at = perf_counter()
            export_rig(
                rig, layers, args.output, stack.canvas_width, stack.canvas_height
            )
            logger.info(
                "导出完成，耗时 %.2f 秒，目录 %s",
                perf_counter() - started_at,
                args.output,
            )
        return 0
    except Exception:
        logger.exception("image2live2d 转换失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
