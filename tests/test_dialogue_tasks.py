import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.audio.extract import extract_audio
from app.core.config import Settings
from app.dialogue.transcribe import DialogueTranscriber, Sentence
from app.main import app

ENDPOINT = "/internal/v1/asset-extraction/tasks"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def shared_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("对白任务测试需要 FFmpeg 和 FFprobe")
    root = tmp_path / "input"
    root.mkdir()
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=red:size=96x64:rate=10:duration=2",
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s16le",
            str(root / "delayed.mkv"),
        ],
        check=True,
        timeout=15,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=red:size=96x64:rate=10:duration=1",
            "-c:v",
            "mpeg4",
            str(root / "mute.mp4"),
        ],
        check=True,
        timeout=15,
    )
    output = tmp_path / "output"
    monkeypatch.setenv("CHARACTER_INPUT_ROOT", str(root))
    monkeypatch.setenv("CHARACTER_OUTPUT_ROOT", str(output))
    monkeypatch.setenv("FFMPEG_PATH", ffmpeg)
    monkeypatch.setenv("FFPROBE_PATH", ffprobe)
    return output


async def wait_terminal(client: AsyncClient, task: str) -> dict:
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"{ENDPOINT}/{task}")
            assert response.status_code == 200
            state = response.json()
            if state["status"] not in {"QUEUED", "RUNNING"}:
                return state
            await asyncio.sleep(0.02)


@pytest.mark.anyio
async def test_audio_and_dialogue_reuse_wav_and_restore_video_timestamps(
    shared_output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def count_extraction(source: Path, output: Path, settings: Settings) -> None:
        calls.append(source)
        extract_audio(source, output, settings)

    def transcribe(audio: Path) -> list[Sentence]:
        assert audio.is_relative_to(shared_output)
        assert audio.stat().st_size > 44
        return [
            Sentence(start=400, end=700, text=" 第二句 ", spk=0),
            Sentence(start=100, end=450, text="第一句", spk=9),
            Sentence(start=700, end=900, text="未知说话人"),
        ]

    monkeypatch.setattr("app.pipeline.asset_tasks.extract_audio", count_extraction)
    monkeypatch.setattr(
        "app.pipeline.dialogue_extraction.extract_audio", count_extraction
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        monkeypatch.setattr(app.state.asset_tasks, "transcribe_dialogue", transcribe)
        request = {
            "taskId": "both",
            "sourceVideoRef": "delayed.mkv",
            "outputs": ["DIALOGUE", "AUDIO"],
        }
        accepted = await client.post(ENDPOINT, json=request)
        assert accepted.status_code == 202
        assert (
            await client.post(
                ENDPOINT, json={**request, "outputs": ["AUDIO", "DIALOGUE"]}
            )
        ).status_code == 202
        state = await wait_terminal(client, "both")
        assert state["status"] == "SUCCEEDED", state
        branches = state["branches"]
        assert (shared_output / branches["AUDIO"]["result"]["audioRef"]).is_file()
        result = branches["DIALOGUE"]["result"]
        assert result["segmentCount"] == 3
        manifest = json.loads((shared_output / result["manifestRef"]).read_text())
        assert manifest == {
            "schemaVersion": 1,
            "durationMs": 2000,
            "segments": [
                {
                    "segmentIndex": 1,
                    "startMs": 600,
                    "endMs": 950,
                    "text": "第一句",
                    "speakerId": "speaker-1",
                },
                {
                    "segmentIndex": 2,
                    "startMs": 900,
                    "endMs": 1200,
                    "text": "第二句",
                    "speakerId": "speaker-2",
                },
                {
                    "segmentIndex": 3,
                    "startMs": 1200,
                    "endMs": 1400,
                    "text": "未知说话人",
                    "speakerId": None,
                },
            ],
        }
        assert len(calls) == 1
        assert (await client.post(ENDPOINT, json=request)).json() == state


@pytest.mark.anyio
async def test_empty_audio_and_silence_and_partial_success(
    shared_output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_speech(audio: Path) -> list[Sentence]:
        return []

    def cannot_transcribe(audio: Path) -> list[Sentence]:
        raise RuntimeError("模拟模型推理失败")

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        monkeypatch.setattr(app.state.asset_tasks, "transcribe_dialogue", no_speech)
        request = {
            "taskId": "mute",
            "sourceVideoRef": "mute.mp4",
            "outputs": ["DIALOGUE"],
        }
        assert (await client.post(ENDPOINT, json=request)).status_code == 202
        state = await wait_terminal(client, "mute")
        assert state["status"] == "FAILED"
        assert (
            state["branches"]["DIALOGUE"]["error"]["code"] == "AUDIO_STREAM_NOT_FOUND"
        )

        request = {**request, "taskId": "silence", "sourceVideoRef": "delayed.mkv"}
        assert (await client.post(ENDPOINT, json=request)).status_code == 202
        state = await wait_terminal(client, "silence")
        assert state["status"] == "SUCCEEDED"
        result = state["branches"]["DIALOGUE"]["result"]
        assert result["segmentCount"] == 0
        directory = (shared_output / result["manifestRef"]).parent
        assert sorted(p.name for p in directory.iterdir()) == ["dialogue.json"]

        monkeypatch.setattr(
            app.state.asset_tasks, "transcribe_dialogue", cannot_transcribe
        )
        request = {**request, "taskId": "broken", "outputs": ["DIALOGUE", "AUDIO"]}
        assert (await client.post(ENDPOINT, json=request)).status_code == 202
        state = await wait_terminal(client, "broken")
        assert state["status"] == "PARTIALLY_SUCCEEDED"
        assert (
            state["branches"]["DIALOGUE"]["error"]["code"]
            == "DIALOGUE_TRANSCRIPTION_FAILED"
        )
        assert (
            shared_output / state["branches"]["AUDIO"]["result"]["audioRef"]
        ).is_file()
        assert not list(shared_output.glob("broken-dialogue-*"))


def test_failed_speaker_model_preserves_timed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TextModel:
        def generate(
            self, *, input: str, sentence_timestamp: bool, batch_size_s: int
        ) -> object:
            return [
                {
                    "text": "试音",
                    "sentence_info": [{"start": 20, "end": 150, "sentence": "试音"}],
                }
            ]

    def load(speaker: bool) -> TextModel:
        if speaker:
            raise RuntimeError("模拟 CAM++ 不可用")
        return TextModel()

    transcriber = DialogueTranscriber(Settings(dialogue_speaker_model="cam++"))
    monkeypatch.setattr(transcriber, "load_model", load)
    assert transcriber.transcribe(Path("audio.wav")) == [
        Sentence(start=20, end=150, text="试音")
    ]
