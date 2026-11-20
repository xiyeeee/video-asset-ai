from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    grounding_dino_model: str = "IDEA-Research/grounding-dino-tiny"
    sam2_model: str = "facebook/sam2.1-hiera-tiny"
    character_device: str = "cpu"
    character_input_root: Path = Path("samples")
    character_output_root: Path = Path("outputs/character-tasks")
    character_frame_interval_seconds: float = Field(
        default=2, gt=0, allow_inf_nan=False
    )
    shot_scene_threshold: float = Field(default=10, gt=0, le=100, allow_inf_nan=False)
    shot_ignore_bottom_fraction: float = Field(
        default=0.2, ge=0, lt=1, allow_inf_nan=False
    )
    dialogue_asr_model: str = "paraformer-zh"
    dialogue_vad_model: str = "fsmn-vad"
    dialogue_punctuation_model: str = "ct-punc"
    dialogue_speaker_model: str = "cam++"
