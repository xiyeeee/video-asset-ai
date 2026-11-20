import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient, Response
from pytest import MonkeyPatch

from app.main import app


async def request_health() -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/health")


def test_health_with_available_tools(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.health.tool_available", lambda _: True)
    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "ffmpegAvailable": True,
        "ffprobeAvailable": True,
    }


def test_health_with_missing_tools(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FFMPEG_PATH", str(tmp_path / "missing-ffmpeg"))
    monkeypatch.setenv("FFPROBE_PATH", str(tmp_path / "missing-ffprobe"))
    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "ffmpegAvailable": False,
        "ffprobeAvailable": False,
    }
