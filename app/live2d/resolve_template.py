from collections import defaultdict
from time import perf_counter

from app.core.config import Settings
from app.live2d.analysis_models import (
    AnalysisDiagnostic,
    ResolvedTrack,
    TemplateRequest,
    TemplateResult,
)
from app.live2d.compile_motion import parameter_value
from app.live2d.model_analysis import analyze_model
from app.live2d.motion_models import MotionError


def resolve_template(request: TemplateRequest, settings: Settings) -> TemplateResult:
    started = perf_counter()
    model = analyze_model(request, settings)
    if (
        request.expected_asset_fingerprint
        and request.expected_asset_fingerprint != model.asset_fingerprint
    ):
        raise MotionError(
            "MODEL_VERSION_MISMATCH", "能力分析所用资产已变化，请重新分析"
        )
    capabilities = {
        item.id: item for group in model.capabilities for item in group.capabilities
    }
    diagnostics = list(model.diagnostics)
    tracks: list[ResolvedTrack] = []
    occupied: dict[str, list[ResolvedTrack]] = defaultdict(list)
    for track in request.tracks:
        target = capabilities.get(track.semantic_id)
        binding = target.binding if target is not None else None
        if target is None or target.status != "CONFIRMED" or binding is None:
            diagnostics.append(
                AnalysisDiagnostic(
                    code="BINDING_UNCONFIRMED",
                    message=target.reason if target else "未找到对应的能力语义",
                    semantic_id=track.semantic_id,
                )
            )
            continue
        if binding.asset_ref is not None:
            if (
                track.end_value is not None
                or track.start_value != 0
                or track.amplitude != 1
                or track.easing != "LINEAR"
            ):
                diagnostics.append(
                    AnalysisDiagnostic(
                        code="ASSET_TRACK_VALUES_UNSUPPORTED",
                        message="现有动作文件使用自身曲线",
                        semantic_id=track.semantic_id,
                    )
                )
                continue
            asset = next(
                item for item in target.assets if item.artifact_ref == binding.asset_ref
            )
            resolved = ResolvedTrack(
                semantic_id=track.semantic_id,
                start_ms=track.start_ms,
                duration_ms=track.duration_ms,
                asset=asset,
            )
            used_ids = asset.parameter_ids
        else:
            parameter = next(
                item for item in target.parameters if item.id == binding.parameter_id
            )
            minimum, neutral, maximum = (
                parameter.min,
                parameter.default_value,
                parameter.max,
            )
            if (
                minimum is None
                or neutral is None
                or maximum is None
                or not minimum < maximum
            ):
                diagnostics.append(
                    AnalysisDiagnostic(
                        code="PARAMETER_RANGE_UNKNOWN",
                        message="缺少有效的参数范围和默认值",
                        semantic_id=track.semantic_id,
                        reference=binding.parameter_id,
                    )
                )
                continue
            if track.end_value is None:
                diagnostics.append(
                    AnalysisDiagnostic(
                        code="PARAMETER_TRACK_VALUES_REQUIRED",
                        message="参数轨道需要归一化结束值",
                        semantic_id=track.semantic_id,
                    )
                )
                continue
            start, end = (
                value * binding.direction * track.amplitude
                for value in (track.start_value, track.end_value)
            )
            if (
                min(start, end) < 0
                and (minimum == neutral or parameter.active_decreasing is False)
            ) or (
                max(start, end) > 0
                and (maximum == neutral or parameter.active_increasing is False)
            ):
                diagnostics.append(
                    AnalysisDiagnostic(
                        code="BINDING_DIRECTION_UNAVAILABLE",
                        message="参数在请求的方向上无法驱动",
                        semantic_id=track.semantic_id,
                        reference=binding.parameter_id,
                    )
                )
                continue
            resolved = ResolvedTrack(
                semantic_id=track.semantic_id,
                start_ms=track.start_ms,
                duration_ms=track.duration_ms,
                parameter_id=parameter.id,
                easing=track.easing,
                start_value=parameter_value(minimum, neutral, maximum, start),
                end_value=parameter_value(minimum, neutral, maximum, end),
            )
            used_ids = [parameter.id]
        for parameter_id in used_ids:
            occupied[parameter_id].append(resolved)
        tracks.append(resolved)

    collision = False
    for parameter_id, segments in occupied.items():
        segments.sort(key=lambda segment: segment.start_ms)
        for left, right in zip(segments, segments[1:]):
            if left.start_ms + left.duration_ms > right.start_ms:
                collision = True
                diagnostics.append(
                    AnalysisDiagnostic(
                        code="PARAMETER_CONFLICT",
                        message=f"{left.semantic_id} 与 {right.semantic_id} 占用同一参数",
                        semantic_id=right.semantic_id,
                        reference=parameter_id,
                    )
                )
    ready = len(tracks) == len(request.tracks) and not collision
    return TemplateResult(
        model_version_id=request.model_version_id,
        binding_version_id=request.binding_version_id,
        asset_fingerprint=model.asset_fingerprint,
        template_id=request.template_id,
        duration_ms=request.duration_ms,
        status="READY" if ready else "UNRESOLVED",
        tracks=tracks if ready else [],
        diagnostics=diagnostics,
        elapsed_ms=round((perf_counter() - started) * 1000),
    )
