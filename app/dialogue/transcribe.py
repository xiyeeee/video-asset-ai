import logging
from functools import cached_property
from pathlib import Path
from typing import Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter

from app.core.config import Settings

logger = logging.getLogger(__name__)


class DialogueModelUnavailable(RuntimeError):
    """基础对白模型无法加载。"""


class Sentence(BaseModel, frozen=True):
    start: float = Field(allow_inf_nan=False)
    end: float = Field(allow_inf_nan=False)
    text: str = Field(validation_alias=AliasChoices("text", "sentence"))
    spk: int | None = None


class Recording(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    sentence_info: list[Sentence] = Field(default_factory=list)


recordings = TypeAdapter(list[Recording])


class FunAsrModel(Protocol):
    def generate(
        self, *, input: str, sentence_timestamp: bool, batch_size_s: int
    ) -> object: ...


class DialogueTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.speaker_enabled = bool(settings.dialogue_speaker_model)

    def load_model(self, speaker: bool) -> FunAsrModel:
        from funasr import AutoModel

        options = {"spk_model": self.settings.dialogue_speaker_model} if speaker else {}
        return AutoModel(
            model=self.settings.dialogue_asr_model,
            vad_model=self.settings.dialogue_vad_model,
            punc_model=self.settings.dialogue_punctuation_model,
            device="cpu",
            disable_update=True,
            trust_remote_code=False,
            **options,
        )

    @cached_property
    def with_speakers(self) -> FunAsrModel:
        return self.load_model(speaker=True)

    @cached_property
    def with_text_only(self) -> FunAsrModel:
        try:
            return self.load_model(speaker=False)
        except Exception as exc:
            raise DialogueModelUnavailable(
                "无法加载 FunASR 转写、端点或标点模型"
            ) from exc

    def transcribe(self, audio: Path) -> list[Sentence]:
        if self.speaker_enabled:
            try:
                return self.sentences(self.with_speakers, audio)
            except Exception:
                logger.warning("带说话人的对白识别失败，回退到文本转写", exc_info=True)
                self.speaker_enabled = False
                self.__dict__.pop("with_speakers", None)
        return self.sentences(self.with_text_only, audio)

    @staticmethod
    def sentences(model: FunAsrModel, audio: Path) -> list[Sentence]:
        data = recordings.validate_python(
            model.generate(input=str(audio), sentence_timestamp=True, batch_size_s=60)
        )
        if any(result.text.strip() and not result.sentence_info for result in data):
            raise ValueError("FunASR 返回转写文本但没有句级时间戳")
        return [sentence for result in data for sentence in result.sentence_info]
