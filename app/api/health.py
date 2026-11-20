from fastapi import APIRouter

from app.core.config import Settings
from app.video.ffmpeg import tool_available

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str | bool]:
    settings = Settings()
    ffmpeg_available = tool_available(settings.ffmpeg_path)
    ffprobe_available = tool_available(settings.ffprobe_path)
    return {
        "status": "ok" if ffmpeg_available and ffprobe_available else "degraded",
        "ffmpegAvailable": ffmpeg_available,
        "ffprobeAvailable": ffprobe_available,
    }
