import json
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from app.audio.extract import AudioStreamNotFound, extract_audio
from app.core.config import Settings
from app.dialogue.transcribe import Sentence
from app.pipeline.asset_task_models import DialogueResult, DialogueStage
from app.storage.shared_files import publish_task_directory, stage_task_directory
from app.video.ffmpeg import probe_video, run_tool

Transcribe = Callable[[Path], list[Sentence]]


class DialogueSegment(BaseModel):
    segment_index: int = Field(serialization_alias="segmentIndex")
    start_ms: int = Field(serialization_alias="startMs")
    end_ms: int = Field(serialization_alias="endMs")
    text: str
    speaker_id: str | None = Field(serialization_alias="speakerId")


class DialogueManifest(BaseModel):
    schema_version: int = Field(1, serialization_alias="schemaVersion")
    duration_ms: int = Field(serialization_alias="durationMs")
    segments: list[DialogueSegment]


def probe_timeline(source: Path, settings: Settings) -> tuple[int, Decimal]:
    duration = Decimal(str(probe_video(source, settings).duration_seconds)) * 1000
    result = run_tool(
        settings.ffprobe_path,
        [
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=start_time:format=start_time",
            "-of",
            "json",
            str(source),
        ],
        timeout=30,
    )
    timing = json.loads(result.stdout)
    if not timing.get("streams"):
        raise AudioStreamNotFound("源视频没有音轨")
    origin = timing.get("format", {}).get("start_time", "0")
    if origin == "N/A":
        origin = "0"
    audio_start = timing["streams"][0].get("start_time", origin)
    if audio_start == "N/A":
        audio_start = origin
    return int(duration.to_integral_value(rounding=ROUND_CEILING)), (
        Decimal(audio_start) - Decimal(origin)
    ) * 1000


def build_manifest(
    sentences: list[Sentence], duration_ms: int, audio_offset_ms: Decimal
) -> DialogueManifest:
    def video_ms(value: float) -> int:
        # FunASR 已将 VAD 切段还原到 WAV；这里仅加音轨相对视频起点的偏移。
        timestamp = Decimal(str(value)) + audio_offset_ms
        return int(timestamp.to_integral_value(rounding=ROUND_HALF_UP))

    candidates = []
    for sentence in sentences:
        text = sentence.text.strip()
        if not text:
            continue
        if sentence.start < 0 or sentence.end <= sentence.start:
            raise ValueError("对白模型返回无效句段时间")
        start = max(0, video_ms(sentence.start))
        end = min(duration_ms, video_ms(sentence.end))
        if start < end:
            candidates.append((start, end, text, sentence.spk))
    candidates.sort(key=lambda entry: (entry[0], entry[1]))
    speakers: dict[int, str] = {}
    segments = []
    for index, (start, end, text, speaker) in enumerate(candidates, 1):
        speaker_id = None
        if speaker is not None and speaker >= 0:
            speaker_id = speakers.setdefault(speaker, f"speaker-{len(speakers) + 1}")
        segments.append(
            DialogueSegment(
                segment_index=index,
                start_ms=start,
                end_ms=end,
                text=text,
                speaker_id=speaker_id,
            )
        )
    return DialogueManifest(duration_ms=duration_ms, segments=segments)


def extract_dialogue(
    task_id: str,
    source: Path,
    output_root: Path,
    settings: Settings,
    transcribe: Transcribe,
    report: Callable[[DialogueStage], None],
    prepare_audio: Callable[[], Path] | None = None,
) -> DialogueResult:
    report(DialogueStage.PREPARING_AUDIO)
    duration_ms, audio_offset = probe_timeline(source, settings)
    audio = prepare_audio() if prepare_audio is not None else None
    with stage_task_directory(output_root) as temporary:
        if audio is None:
            audio = Path(temporary) / "audio.wav"
            extract_audio(source, audio, settings)
        report(DialogueStage.TRANSCRIBING_DIALOGUE)
        sentences = transcribe(audio)
        report(DialogueStage.WRITING_RESULTS)
        manifest = build_manifest(sentences, duration_ms, audio_offset)
        staged = Path(temporary) / "result"
        staged.mkdir()
        (staged / "dialogue.json").write_text(
            manifest.model_dump_json(indent=2, by_alias=True) + "\n", encoding="utf-8"
        )
        destination = output_root / f"{task_id}-dialogue-{uuid4().hex}"
        publish_task_directory(output_root, staged, destination)
    return DialogueResult(
        manifest_ref=(destination / "dialogue.json")
        .relative_to(output_root)
        .as_posix(),
        segment_count=len(manifest.segments),
    )
