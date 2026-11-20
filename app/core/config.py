from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    grounding_dino_model: str = "IDEA-Research/grounding-dino-tiny"
    sam2_model: str = "facebook/sam2.1-hiera-tiny"
    character_device: str = "cpu"
