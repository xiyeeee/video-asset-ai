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
    from image2live2d.irr.schema import Rig

logger = logging.getLogger(__name__)


def export_rig(rig: Rig, layers: Path, output: Path) -> None:
    from image2live2d.backends.live2d import cdi3, model3, physics3
    from image2live2d.backends.live2d.moc3_binary import write_moc3
    from image2live2d.backends.live2d.moc3_emit import build_atlas, rig_to_moc3

    output.mkdir(parents=True)
    (output / "textures").mkdir()
    atlas, texture_coordinates = build_atlas(rig, layers)
    atlas.save(output / "textures" / "atlas.png")
    (output / "model.moc3").write_bytes(
        write_moc3(rig_to_moc3(rig, atlas_uv=texture_coordinates))
    )
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
            logger.info("绑定完成，耗时 %.2f 秒", perf_counter() - started_at)
            started_at = perf_counter()
            export_rig(rig, layers, args.output)
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
