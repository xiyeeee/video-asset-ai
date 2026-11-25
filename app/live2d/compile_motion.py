import hashlib
import json
import math
import subprocess
from array import array
from collections import defaultdict
from pathlib import Path
from sys import byteorder
from typing import TypedDict

from app.core.config import Settings
from app.live2d.motion_models import (
    Capabilities,
    Clip,
    Diagnostic,
    MotionError,
    MotionRequest,
    Parameter,
)
from app.storage.shared_files import InvalidSourceReference, resolve_source_file

Point = tuple[int, float]
Tracks = dict[str, list[Point]]
SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
MAX_SAVED_BYTES = 1024 * 1024
PRESETS = {
    "blink": [(0.0, 0.0), (0.38, 1.0), (0.52, 1.0), (1.0, 0.0)],
    "smile": [(0.0, 0.0), (0.28, 0.75), (0.75, 0.75), (1.0, 0.0)],
    "nod": [(0.0, 0.0), (0.40, 0.65), (0.56, 0.65), (1.0, 0.0)],
}


class Curve(TypedDict):
    Target: str
    Id: str
    Segments: list[float]


class MotionFile(TypedDict):
    Version: int
    Meta: dict[str, int | float | bool]
    Curves: list[Curve]


def _value(parameter: Parameter, weight: float, *, decreasing: bool = False) -> float:
    target = parameter.min if decreasing else parameter.max
    return parameter.default_value + (target - parameter.default_value) * weight


def _shape(
    duration_ms: int,
    fractions: list[tuple[float, float]],
    parameter: Parameter,
    *,
    decreasing: bool,
) -> list[Point]:
    # 片段边界始终写默认值。短片段量化到毫秒时，合并碰到同一毫秒的点。
    points = {
        round(fraction * duration_ms): _value(parameter, weight, decreasing=decreasing)
        for fraction, weight in fractions
    }
    points[0] = points[duration_ms] = parameter.default_value
    return sorted(points.items())


def _source(root: Path, clip: Clip) -> Path:
    if clip.source_ref is None:
        raise ValueError("片段没有媒体引用")
    try:
        return resolve_source_file(root, clip.source_ref, reference_field="sourceRef")
    except (InvalidSourceReference, FileNotFoundError) as exc:
        raise MotionError("MOTION_SOURCE_INVALID", "动作或音频文件引用无效") from exc


def speech_points(clip: Clip, parameter: Parameter, settings: Settings) -> list[Point]:
    path = _source(settings.live2d_input_root.resolve(), clip)
    if clip.source_sha256 is not None:
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != clip.source_sha256:
                raise MotionError(
                    "MOTION_SOURCE_VERSION_MISMATCH", "音频摘要与请求不一致"
                )
    try:
        pcm = subprocess.run(
            [
                settings.ffmpeg_path,
                "-v",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
                "-ss",
                str(clip.source_start_ms / 1000),
                "-map",
                "0:a:0",
                "-t",
                str(clip.duration_ms / 1000),
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "s16le",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout
    except FileNotFoundError as exc:
        raise MotionError("FFMPEG_UNAVAILABLE", "找不到 FFmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise MotionError("AUDIO_DECODE_TIMEOUT", "台词音频解码超时") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise MotionError("AUDIO_DECODE_FAILED", "无法解码台词音频") from exc
    if not pcm:
        raise MotionError("AUDIO_DECODE_FAILED", "台词音频没有样本")

    samples = array("h", pcm)
    if byteorder != "little":
        samples.byteswap()
    default = parameter.default_value
    points: dict[int, float] = {0: default, clip.duration_ms: default}
    audio_end = min(clip.duration_ms, math.ceil(len(samples) * 1000 / SAMPLE_RATE))
    # 窗口中点对齐 RMS。音频短于片段时，从音频结束处恢复默认口型。
    for index in range(0, len(samples), FRAME_SAMPLES):
        frame = samples[index : index + FRAME_SAMPLES]
        ms = round((index + len(frame) / 2) * 1000 / SAMPLE_RATE)
        if 0 < ms < audio_end:
            rms = math.sqrt(sum(value * value for value in frame) / len(frame)) / 32768
            gain = min(1.0, max(0.0, (rms - 0.015) / 0.235))
            points[ms] = _value(parameter, gain)
    points[audio_end] = default
    return sorted(points.items())


def saved_tracks(
    clip: Clip, settings: Settings, parameters: dict[str, Parameter]
) -> Tracks:
    path = _source(settings.live2d_output_root.resolve(), clip)
    with path.open("rb") as stream:
        payload = stream.read(MAX_SAVED_BYTES + 1)
    if len(payload) > MAX_SAVED_BYTES:
        raise MotionError("SAVED_ACTION_INVALID", "已保存动作文件过大")
    if hashlib.sha256(payload).hexdigest() != clip.source_sha256:
        raise MotionError(
            "MOTION_SOURCE_VERSION_MISMATCH", "已保存动作摘要与请求不一致"
        )
    try:
        motion = json.loads(payload)
        duration = float(motion["Meta"]["Duration"])
        if motion["Version"] != 3 or not math.isfinite(duration) or duration <= 0:
            raise ValueError("已保存动作版本或时长无效")
        tracks: Tracks = {}
        for curve in motion["Curves"]:
            parameter_id = curve["Id"]
            if curve["Target"] != "Parameter" or parameter_id in tracks:
                raise ValueError("已保存动作须只含唯一参数轨")
            if parameter_id not in parameters or not parameters[parameter_id].active:
                raise MotionError(
                    "CAPABILITY_MISSING",
                    "模型缺少已保存动作所需的绑定",
                    [
                        Diagnostic(
                            code="CAPABILITY_MISSING",
                            message="模型缺少有效参数绑定",
                            clip_ids=[clip.clip_id],
                            parameter_ids=[parameter_id],
                        )
                    ],
                )
            parameter = parameters[parameter_id]
            segments = curve["Segments"]
            if len(segments) < 2 or (len(segments) - 2) % 3 or segments[0] != 0:
                raise ValueError("已保存动作线性曲线结构无效")
            knots = [(0.0, segments[1])]
            for index in range(2, len(segments), 3):
                kind, time, value = segments[index : index + 3]
                if kind != 0 or not knots[-1][0] < time <= duration:
                    raise ValueError("首版只支持递增的线性参数曲线")
                knots.append((time, value))
            if any(
                not math.isfinite(value) or not parameter.min <= value <= parameter.max
                for _, value in knots
            ):
                raise ValueError("曲线超出当前模型参数范围")
            tail_ms = min(120, max(1, clip.duration_ms // 4))
            body_ms = clip.duration_ms - tail_ms
            # 保留保存动作的内部关键帧，并在片段内留出淡入与恢复窗口。
            points = {
                max(1, round(time / duration * body_ms)): value for time, value in knots
            }
            points[body_ms] = knots[-1][1]
            points[0] = points[clip.duration_ms] = parameter.default_value
            tracks[parameter_id] = sorted(points.items())
        if not tracks:
            raise ValueError("已保存动作没有参数曲线")
        return tracks
    except MotionError:
        raise
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        raise MotionError(
            "SAVED_ACTION_INVALID", "已保存动作须为可用的线性 Cubism 参数动作"
        ) from exc


def emit_motion(duration_ms: int, tracks: Tracks) -> MotionFile:
    curves: list[Curve] = []
    total_points = total_segments = 0
    for parameter_id, points in sorted(tracks.items()):
        first, *rest = points
        segments = [first[0] / 1000, first[1]]
        for time_ms, value in rest:
            segments.extend((0, time_ms / 1000, value))
        curves.append({"Target": "Parameter", "Id": parameter_id, "Segments": segments})
        total_points += len(points)
        total_segments += len(rest)
    return {
        "Version": 3,
        "Meta": {
            "Duration": duration_ms / 1000,
            "Fps": 30,
            "Loop": False,
            "AreBeziersRestricted": True,
            "CurveCount": len(curves),
            "TotalSegmentCount": total_segments,
            "TotalPointCount": total_points,
            "UserDataCount": 0,
            "TotalUserDataSize": 0,
            "FadeInTime": 0,
            "FadeOutTime": 0,
        },
        "Curves": curves,
    }


def compile_motion(
    request: MotionRequest, model: Capabilities, settings: Settings
) -> MotionFile:
    parameters = {parameter.id: parameter for parameter in model.parameters}
    capabilities = {capability.id: capability for capability in model.capabilities}
    diagnostics: list[Diagnostic] = []
    assignments: dict[str, list[Clip]] = defaultdict(list)
    clip_tracks: dict[str, Tracks] = {}

    # 先分配参数所有权，冲突与缺绑定的请求不用等待音频解码。
    for clip in request.clips:
        if clip.track_type == "SAVED_ACTION":
            try:
                tracks = saved_tracks(clip, settings, parameters)
            except MotionError as exc:
                diagnostics.extend(
                    exc.diagnostics
                    or [
                        Diagnostic(
                            code=exc.code, message=str(exc), clip_ids=[clip.clip_id]
                        )
                    ]
                )
                continue
            clip_tracks[clip.clip_id] = tracks
            for parameter_id in tracks:
                assignments[parameter_id].append(clip)
            continue

        action = "speech" if clip.track_type == "SPEECH_AUDIO" else clip.source_id
        capability = capabilities.get(action)
        if capability is None:
            diagnostics.append(
                Diagnostic(
                    code="PRESET_UNKNOWN",
                    message="没有对应的动作预设",
                    clip_ids=[clip.clip_id],
                )
            )
        elif not capability.supported:
            diagnostics.append(
                Diagnostic(
                    code="CAPABILITY_MISSING",
                    message="模型缺少动作所需的绑定",
                    clip_ids=[clip.clip_id],
                    capability_id=action,
                    parameter_ids=capability.missing_parameter_ids,
                )
            )
        elif clip.track_type == "PRESET" and action not in PRESETS:
            diagnostics.append(
                Diagnostic(
                    code="PRESET_UNAVAILABLE",
                    message="该绑定尚无可用的动作预设",
                    clip_ids=[clip.clip_id],
                    capability_id=action,
                )
            )
        else:
            for parameter_id in capability.parameter_ids:
                assignments[parameter_id].append(clip)

    for parameter_id, clips in sorted(assignments.items()):
        clips.sort(key=lambda clip: (clip.start_ms, clip.clip_id))
        for index, first in enumerate(clips):
            for second in clips[index + 1 :]:
                if second.start_ms >= first.end_ms:
                    break
                diagnostics.append(
                    Diagnostic(
                        code="PARAMETER_CONFLICT",
                        message="重叠片段同时控制同一个参数",
                        clip_ids=[first.clip_id, second.clip_id],
                        parameter_ids=[parameter_id],
                        start_ms=second.start_ms,
                        end_ms=min(first.end_ms, second.end_ms),
                    )
                )
    if diagnostics:
        raise MotionError(
            "MOTION_COMPILE_REJECTED", "组合动作存在无法编译的片段", diagnostics
        )

    for clip in request.clips:
        if clip.track_type == "SPEECH_AUDIO":
            parameter = parameters[capabilities["speech"].parameter_ids[0]]
            try:
                clip_tracks[clip.clip_id] = {
                    parameter.id: speech_points(clip, parameter, settings)
                }
            except MotionError as exc:
                raise MotionError(
                    exc.code,
                    str(exc),
                    [
                        Diagnostic(
                            code=exc.code, message=str(exc), clip_ids=[clip.clip_id]
                        )
                    ],
                ) from exc
        elif clip.track_type == "PRESET":
            clip_tracks[clip.clip_id] = {
                parameter_id: _shape(
                    clip.duration_ms,
                    PRESETS[clip.source_id],
                    parameters[parameter_id],
                    decreasing=clip.source_id in {"blink", "nod"},
                )
                for parameter_id in capabilities[clip.source_id].parameter_ids
            }

    combined: Tracks = {}
    for parameter_id, clips in assignments.items():
        default = parameters[parameter_id].default_value
        points = {0: default, request.duration_ms: default}
        for clip in clips:
            for offset, value in clip_tracks[clip.clip_id][parameter_id]:
                points[clip.start_ms + offset] = value
        combined[parameter_id] = sorted(points.items())
    return emit_motion(request.duration_ms, combined)
