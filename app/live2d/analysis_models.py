from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.live2d.motion_models import (
    MAX_DURATION_MS,
    Contract,
    Digest,
    FileRef,
    ModelRef,
    VersionId,
)

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
UnitValue = Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
Status = Literal["CONFIRMED", "CANDIDATE", "UNKNOWN", "MISSING"]
AssetKind = Literal["MOTION", "EXPRESSION"]
Easing = Literal["LINEAR", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"]


class Binding(Contract):
    semantic_id: Identifier
    parameter_id: Identifier | None = None
    asset_ref: FileRef | None = None
    asset_sha256: Digest | None = None
    direction: Literal[1, -1] = 1

    @model_validator(mode="after")
    def check_target(self) -> "Binding":
        if (self.parameter_id is None) == (self.asset_ref is None):
            raise ValueError("一条绑定须指定参数或动作文件")
        if self.asset_ref is None and self.asset_sha256 is not None:
            raise ValueError("文件摘要需要文件引用")
        if self.asset_ref is not None and self.direction != 1:
            raise ValueError("动作文件使用自身方向")
        return self


class AnalysisRequest(ModelRef):
    runtime_parameter_ref: FileRef | None = None
    binding_version_id: VersionId | None = None
    bindings: list[Binding] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def check_bindings(self) -> "AnalysisRequest":
        if self.bindings and self.binding_version_id is None:
            raise ValueError("确认映射需要绑定版本")
        if len({binding.semantic_id for binding in self.bindings}) != len(
            self.bindings
        ):
            raise ValueError("同一语义只能配置一条确认映射")
        return self


class AnalysisDiagnostic(Contract):
    code: str
    message: str
    semantic_id: str | None = None
    reference: str | None = None


class ParameterEvidence(Contract):
    id: str
    name: str | None = None
    min: float | None = Field(default=None, allow_inf_nan=False)
    max: float | None = Field(default=None, allow_inf_nan=False)
    default_value: float | None = Field(default=None, allow_inf_nan=False)
    active: bool | None = None
    active_increasing: bool | None = None
    active_decreasing: bool | None = None
    evidence: list[str] = Field(default_factory=list)


class ModelAsset(Contract):
    artifact_ref: str
    kind: AssetKind
    sha256: Digest
    parameter_ids: list[str]
    duration_ms: int | None = None


class ModelPart(Contract):
    id: str
    name: str
    evidence: Literal["DISPLAY_INFO", "MODEL3_HIT_AREA"]


class Semantic(Contract):
    id: str
    label: str
    status: Status
    required_parameter_ids: list[str]
    parameters: list[ParameterEvidence]
    assets: list[ModelAsset]
    part_ids: list[str]
    evidence: list[str]
    reason: str
    missing_items: list[str]
    binding: Binding | None = None


class CapabilityGroup(Contract):
    id: str
    label: str
    status: Status
    capabilities: list[Semantic]


class AnalysisResult(Contract):
    model_version_id: VersionId
    binding_version_id: VersionId | None
    asset_fingerprint: Digest
    model_moc_sha256: Digest
    parameter_inventory_complete: bool
    parameters: list[ParameterEvidence]
    assets: list[ModelAsset]
    parts: list[ModelPart]
    capabilities: list[CapabilityGroup]
    diagnostics: list[AnalysisDiagnostic]
    elapsed_ms: int


class TemplateTrack(Contract):
    semantic_id: Identifier
    start_ms: int = Field(default=0, ge=0)
    duration_ms: int = Field(gt=0, le=MAX_DURATION_MS)
    start_value: UnitValue = 0
    end_value: UnitValue | None = None
    amplitude: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)
    easing: Easing = "LINEAR"


class TemplateRequest(AnalysisRequest):
    template_id: Identifier
    binding_version_id: VersionId
    expected_asset_fingerprint: Digest | None = None
    duration_ms: int = Field(gt=0, le=MAX_DURATION_MS)
    tracks: list[TemplateTrack] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def check_tracks(self) -> "TemplateRequest":
        if any(
            track.start_ms + track.duration_ms > self.duration_ms
            for track in self.tracks
        ):
            raise ValueError("模板轨道超出总时长")
        return self


class ResolvedTrack(Contract):
    semantic_id: str
    start_ms: int
    duration_ms: int
    parameter_id: str | None = None
    start_value: float | None = None
    end_value: float | None = None
    easing: Easing | None = None
    asset: ModelAsset | None = None


class TemplateResult(Contract):
    model_version_id: VersionId
    binding_version_id: VersionId
    asset_fingerprint: Digest
    template_id: str
    duration_ms: int
    status: Literal["READY", "UNRESOLVED"]
    tracks: list[ResolvedTrack]
    diagnostics: list[AnalysisDiagnostic]
    elapsed_ms: int
