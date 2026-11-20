import argparse
import logging
from pathlib import Path

from app.core.config import Settings
from app.video.ffmpeg import VideoError, extract_frames

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按播放时间间隔抽取视频截图")
    parser.add_argument("--input", required=True, type=Path, help="输入视频文件")
    parser.add_argument("--output", required=True, type=Path, help="截图输出目录")
    parser.add_argument(
        "--interval-seconds", required=True, type=float, help="抽帧间隔（秒）"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        extract_frames(args.input, args.output, args.interval_seconds, Settings())
    except (VideoError, OSError, ValueError) as exc:
        logger.error("抽帧失败，输入 %s，%s", args.input, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
