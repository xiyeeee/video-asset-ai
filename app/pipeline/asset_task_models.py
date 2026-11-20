from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

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
    DIALOGUE = "DIALOGUE"
    BACKGROUND = "BACKGROUND"


class AssetTaskRequest(TaskRequest):
    outputs: frozenset[AssetKind] = Field(min_length=1)
    shot_manifest_ref: str | None = Field(
        default=None, alias="shotManifestRef", min_length=1, max_length=4096
    )

    @model_validator(mode="after")
    def check_shot_ref(self) -> Self:
        if self.shot_manifest_ref is not None and (
            AssetKind.BACKGROUND not in self.outputs or AssetKind.SHOTS in self.outputs
        ):
            raise ValueError("shotManifestRef 仅用于没有同时请求 SHOTS 的背景任务")
        return self


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


class DialogueStage(StrEnum):
    PREPARING_AUDIO = "PREPARING_AUDIO"
    TRANSCRIBING_DIALOGUE = "TRANSCRIBING_DIALOGUE"
    WRITING_RESULTS = "WRITING_RESULTS"


class DialogueResult(BaseModel, frozen=True):
    manifest_ref: str = Field(serialization_alias="manifestRef")
    segment_count: int = Field(serialization_alias="segmentCount")


class BackgroundStage(StrEnum):
    RESOLVING_SHOTS = "RESOLVING_SHOTS"
    SELECTING_BACKGROUND_FRAMES = "SELECTING_BACKGROUND_FRAMES"
    WRITING_RESULTS = "WRITING_RESULTS"


class BackgroundResult(BaseModel, frozen=True):
    manifest_ref: str = Field(serialization_alias="manifestRef")
    background_count: int = Field(serialization_alias="backgroundCount")


BranchStage = TaskStage | AudioStage | ShotStage | DialogueStage | BackgroundStage
BranchResult = TaskResult | AudioResult | ShotResult | DialogueResult | BackgroundResult


class BranchState(BaseModel, frozen=True):
    status: TaskStatus = TaskStatus.QUEUED
    stage: BranchStage | None = None
    progress: TaskProgress | None = None
    result: BranchResult | None = None
    error: TaskError | None = None


class AssetTaskState(BaseModel, frozen=True):
    task_id: TaskId = Field(serialization_alias="taskId")
    status: AssetTaskStatus
    branches: dict[AssetKind, BranchState]
