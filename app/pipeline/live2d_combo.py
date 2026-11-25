from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.live2d.motion_models import Digest, FileRef


class ComboSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    segment_id: str = Field(alias="segmentId", min_length=1, max_length=64)
    track_type: Literal["MOTION", "DIALOGUE"] = Field(alias="trackType")
    source_id: str = Field(alias="sourceId", min_length=1, max_length=1024)
    source_version_id: str = Field(alias="sourceVersionId", min_length=1, max_length=64)
    start_time_ms: int = Field(alias="startTimeMs", ge=0)
    duration_ms: int = Field(alias="durationMs", gt=0)
    source_start_ms: int | None = Field(alias="sourceStartMs", default=None, ge=0)
    source_duration_ms: int | None = Field(alias="sourceDurationMs", default=None, gt=0)
    preset_id: str | None = Field(alias="presetId", default=None)
    source_ref: FileRef | None = Field(alias="sourceRef", default=None)
    source_sha256: Digest | None = Field(alias="sourceSha256", default=None)


class ComboCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: str = Field(
        alias="requestId", min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$"
    )
    project_id: int = Field(alias="projectId", gt=0)
    model_version_id: str = Field(alias="modelVersionId", min_length=1, max_length=64)
    model_manifest_ref: FileRef | None = Field(alias="modelManifestRef", default=None)
    model_moc_sha256: Digest | None = Field(alias="modelMocSha256", default=None)
    name: str = Field(min_length=1, max_length=150)
    duration_ms: int = Field(alias="durationMs", gt=0, le=3_600_000)
    segments: list[ComboSegment] = Field(min_length=1, max_length=1000)


class ComboCompileIssue(BaseModel, frozen=True):
    code: str
    message: str
    segment_ids: list[str] = Field(serialization_alias="segmentIds")


class ComboCompileResult(BaseModel, frozen=True):
    request_id: str = Field(serialization_alias="requestId")
    compiler_version: int = Field(default=1, serialization_alias="compilerVersion")
    status: Literal["READY", "CONFLICT"]
    duration_ms: int = Field(serialization_alias="durationMs")
    issues: list[ComboCompileIssue]
    missing_capabilities: list[str] = Field(serialization_alias="missingCapabilities")
    playback_manifest: dict[str, object] = Field(serialization_alias="playbackManifest")


def compile_combo_motion(
    request: ComboCompileRequest, *, check_motion_overlap: bool = True
) -> ComboCompileResult:
    """校验 Java 时间轴，并为兼容入口保留原有的片段排序。"""
    segments = sorted(
        request.segments,
        key=lambda item: (item.start_time_ms, item.segment_id),
    )
    issues: list[ComboCompileIssue] = []
    missing_capabilities = sorted(
        {
            "MOTION_DURATION_METADATA"
            for segment in segments
            if check_motion_overlap
            and segment.track_type == "MOTION"
            and segment.source_duration_ms is None
        }
    )
    for segment in segments:
        if segment.start_time_ms + segment.duration_ms > request.duration_ms:
            issues.append(
                ComboCompileIssue(
                    code="SEGMENT_OUT_OF_RANGE",
                    message="片段超出组合动作总时长",
                    segment_ids=[segment.segment_id],
                )
            )
        if (
            segment.source_duration_ms is not None
            and segment.duration_ms > segment.source_duration_ms
        ):
            issues.append(
                ComboCompileIssue(
                    code="SOURCE_DURATION_EXCEEDED",
                    message="片段时长超过来源素材能力",
                    segment_ids=[segment.segment_id],
                )
            )

    motions = [
        item
        for item in segments
        if check_motion_overlap and item.track_type == "MOTION"
    ]
    for index, current in enumerate(motions):
        for other in motions[index + 1 :]:
            if other.start_time_ms >= current.start_time_ms + current.duration_ms:
                break
            issues.append(
                ComboCompileIssue(
                    code="MOTION_OVERLAP",
                    message="同一模型的动作片段时间重叠，无法确定 Cubism 动作优先级",
                    segment_ids=[current.segment_id, other.segment_id],
                )
            )

    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "compileMode": "TIMELINE_ONLY",
        "projectId": str(request.project_id),
        "modelVersionId": request.model_version_id,
        "name": request.name,
        "durationMs": request.duration_ms,
        "segments": [
            item.model_dump(
                mode="json",
                by_alias=True,
                exclude={"preset_id", "source_ref", "source_sha256"},
            )
            for item in segments
        ],
    }
    return ComboCompileResult(
        request_id=request.request_id,
        status="CONFLICT" if issues else "READY",
        duration_ms=request.duration_ms,
        issues=issues,
        missing_capabilities=missing_capabilities,
        playback_manifest=manifest,
    )
