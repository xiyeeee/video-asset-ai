from enum import StrEnum

from pydantic import BaseModel, Field

from app.pipeline.character_task_models import (
    TaskError,
    TaskId,
    TaskProgress,
    TaskRequest,
    TaskResult,
    TaskStage,
    TaskStatus,
)


class AssetKind(StrEnum):
    CHARACTERS = "CHARACTERS"
    AUDIO = "AUDIO"
    SHOTS = "SHOTS"


class AssetTaskRequest(TaskRequest):
    outputs: frozenset[AssetKind] = Field(min_length=1)


class AssetTaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"


class AudioStage(StrEnum):
    EXTRACTING_AUDIO = "EXTRACTING_AUDIO"
    WRITING_RESULTS = "WRITING_RESULTS"


class AudioResult(BaseModel, frozen=True):
    audio_ref: str = Field(serialization_alias="audioRef")


class ShotStage(StrEnum):
    DETECTING_SHOTS = "DETECTING_SHOTS"
    EXTRACTING_PREVIEWS = "EXTRACTING_PREVIEWS"
    WRITING_RESULTS = "WRITING_RESULTS"


class ShotResult(BaseModel, frozen=True):
    manifest_ref: str = Field(serialization_alias="manifestRef")
    shot_count: int = Field(serialization_alias="shotCount")


class BranchState(BaseModel, frozen=True):
    status: TaskStatus = TaskStatus.QUEUED
    stage: TaskStage | AudioStage | ShotStage | None = None
    progress: TaskProgress | None = None
    result: TaskResult | AudioResult | ShotResult | None = None
    error: TaskError | None = None


class AssetTaskState(BaseModel, frozen=True):
    task_id: TaskId = Field(serialization_alias="taskId")
    status: AssetTaskStatus
    branches: dict[AssetKind, BranchState]
