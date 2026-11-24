import argparse
import logging
from pathlib import Path
from time import perf_counter

from app.core.config import Settings
from app.live2d.generate import GenerationError, Stage, generate_model
from app.storage.shared_files import publish_task_directory, stage_task_directory

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将本地角色图片转换为 Cubism 运行时模型"
    )
    parser.add_argument("--input", type=Path, required=True, help="PNG、JPEG 或 WebP")
    parser.add_argument("--output", type=Path, required=True, help="尚不存在的输出目录")
    parser.add_argument("--name", help="模型显示名，默认使用图片文件名")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    def report(stage: Stage) -> None:
        logger.debug("CLI 当前阶段 %s", stage)

    started_at = perf_counter()
    try:
        source = args.input.resolve(strict=True)
        output = args.output.parent.resolve() / args.output.name
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"输出目录已存在 {output}")
        with stage_task_directory(output.parent, prefix=".live2d-") as temp:
            workspace = Path(temp)
            staged = workspace / "artifact"
            manifest = generate_model(
                source,
                workspace,
                staged,
                Settings(),
                "cli",
                report,
                model_name=args.name or source.stem,
            )
            publish_task_directory(output.parent, staged, output)
        logger.info("模型已保存 %s；模型包 %s", output / manifest, output / "model.zip")
        return 0
    except GenerationError as exc:
        logger.exception("stage=%s code=%s %s", exc.stage, exc.code, exc)
    except (OSError, ValueError) as exc:
        logger.exception("无法生成或发布模型 %s", exc)
    finally:
        logger.info("CLI 耗时 %.2f 秒", perf_counter() - started_at)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
