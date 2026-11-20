import argparse
import logging
from pathlib import Path

from app.core.config import Settings
from app.video.ffmpeg import VideoError, check_output_directory

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从指定视频截图提取人物透明 PNG")
    parser.add_argument(
        "--frames", required=True, type=Path, help="FFmpeg 抽帧生成的 frames.json"
    )
    parser.add_argument(
        "--frame", required=True, action="append", help="清单内的截图路径，可重复指定"
    )
    parser.add_argument("--output", required=True, type=Path, help="输出目录")
    parser.add_argument(
        "--box-threshold", type=float, default=0.25, help="人物检测分数阈值"
    )
    args = parser.parse_args(argv)
    if not 0 < args.box_threshold <= 1:
        parser.error("--box-threshold 必须大于 0 且不大于 1")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for module in ("httpx", "httpx2"):
        logging.getLogger(module).setLevel(logging.WARNING)
    try:
        from app.characters.extract import extract_characters, select_frames

        frames = select_frames(args.frames, args.frame)
        check_output_directory(args.output)
        from app.characters.models import GroundedSam2

        extract_characters(
            args.frames,
            frames,
            args.output,
            GroundedSam2(Settings(), args.box_threshold),
        )
    except ImportError as exc:
        logger.error("人物提取依赖无法加载，请检查 uv sync --extra characters，%s", exc)
        return 1
    except (OSError, RuntimeError, ValueError, VideoError) as exc:
        logger.error("人物提取失败，输入 %s，%s", args.frames, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
