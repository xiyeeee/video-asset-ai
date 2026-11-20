from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

TaskId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
FrameInterval = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId = Field(alias="taskId")
    source_video_ref: str = Field(alias="sourceVideoRef", min_length=1, max_length=4096)
    frame_interval_seconds: FrameInterval | None = Field(
        default=None, alias="frameIntervalSeconds"
    )


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TaskStage(StrEnum):
    EXTRACTING_FRAMES = "EXTRACTING_FRAMES"
    DETECTING_CHARACTERS = "DETECTING_CHARACTERS"
    SEGMENTING_CHARACTERS = "SEGMENTING_CHARACTERS"
    WRITING_RESULTS = "WRITING_RESULTS"


class TaskProgress(BaseModel, frozen=True):
    frame_count: int | None = Field(default=None, serialization_alias="frameCount")
    detected_character_count: int | None = Field(
        default=None, serialization_alias="detectedCharacterCount"
    )


class TaskResult(BaseModel, frozen=True):
    manifest_ref: str = Field(serialization_alias="manifestRef")
    character_asset_count: int = Field(serialization_alias="characterAssetCount")


class TaskError(BaseModel, frozen=True):
    code: str
    message: str


class TaskState(BaseModel, frozen=True):
    task_id: TaskId = Field(serialization_alias="taskId")
    status: TaskStatus = TaskStatus.QUEUED
    stage: TaskStage | None = None
    progress: TaskProgress = Field(default_factory=TaskProgress)
    result: TaskResult | None = None
    error: TaskError | None = None
