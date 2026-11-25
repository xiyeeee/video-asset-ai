from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.pipeline.character_task_models import TaskId

Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
FileRef = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
VersionId = TaskId | int
MAX_DURATION_MS = 120_000


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: (
            name.split("_")[0] + "".join(part.title() for part in name.split("_")[1:])
        ),
        populate_by_name=True,
        extra="forbid",
        strict=True,
        serialize_by_alias=True,
    )


class ModelRef(Contract):
    model_version_id: VersionId
    model_manifest_ref: FileRef
    model_moc_sha256: Digest | None = None


class Clip(Contract):
    clip_id: TaskId
    track_type: Literal["PRESET", "SPEECH_AUDIO", "SAVED_ACTION"]
    source_id: str = Field(min_length=1, max_length=1024, pattern=r"\S")
    start_ms: int = Field(ge=0, le=MAX_DURATION_MS)
    duration_ms: int = Field(gt=0, le=MAX_DURATION_MS)
    source_start_ms: int = Field(default=0, ge=0, le=86_400_000)
    source_ref: FileRef | None = None
    source_sha256: Digest | None = None

    @model_validator(mode="after")
    def check_source(self) -> "Clip":
        if self.track_type == "PRESET":
            if (
                self.source_ref is not None
                or self.source_sha256 is not None
                or self.source_start_ms
            ):
                raise ValueError("预设只接受 sourceId")
        elif self.source_ref is None or (
            self.track_type == "SAVED_ACTION" and self.source_sha256 is None
        ):
            raise ValueError("媒体需提供 sourceRef，已保存动作还需 sourceSha256")
        if self.track_type == "SAVED_ACTION" and self.source_start_ms:
            raise ValueError("首版已保存动作从开头读取")
        return self

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


class MotionRequest(ModelRef):
    task_id: TaskId
    duration_ms: int = Field(gt=0, le=MAX_DURATION_MS)
    clips: list[Clip] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def check_timeline(self) -> "MotionRequest":
        if len({clip.clip_id for clip in self.clips}) != len(self.clips):
            raise ValueError("clipId 须唯一")
        if any(
            clip.end_ms > self.duration_ms or clip.duration_ms < 2
            for clip in self.clips
        ):
            raise ValueError("片段至少 2 毫秒且须位于组合时间轴内")
        if (
            sum(
                clip.duration_ms
                for clip in self.clips
                if clip.track_type == "SPEECH_AUDIO"
            )
            > MAX_DURATION_MS
        ):
            raise ValueError("台词片段累计不能超过 120 秒")
        return self


class Parameter(Contract):
    id: str
    min: float = Field(allow_inf_nan=False)
    max: float = Field(allow_inf_nan=False)
    default_value: float = Field(allow_inf_nan=False)
    active: bool
    active_decreasing: bool
    active_increasing: bool


class ModelParameters(Contract):
    moc_version: int
    moc_sha256: Digest
    parameters: list[Parameter]


class Capability(Contract):
    id: str
    supported: bool
    parameter_ids: list[str]
    missing_parameter_ids: list[str]


class Capabilities(ModelParameters):
    model_version_id: VersionId
    capabilities: list[Capability]


class Diagnostic(Contract):
    code: str
    message: str
    clip_ids: list[str] = Field(default_factory=list)
    parameter_ids: list[str] = Field(default_factory=list)
    capability_id: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None


class MotionError(ValueError):
    def __init__(
        self, code: str, message: str, diagnostics: list[Diagnostic] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or []
