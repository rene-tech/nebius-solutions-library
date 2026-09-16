"""Strict native wire contracts and reviewed, immutable upstream identities."""

from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PARAKEET = "parakeet-realtime-eou-120m-v1"
MAGPIE = "magpie-tts-multilingual-357m"
SORTFORMER = "diar-streaming-sortformer-4spk-v2-1"
SPEECH_REVISION = "3d91009e8f2ef690112eabeb6907da00ea308d7f"
VOICE_AGENT_REVISION = "a01fa68f0907a52cca1b07115e0e37c68ca580e5"
ModelId = Literal["parakeet-realtime-eou-120m-v1", "magpie-tts-multilingual-357m", "diar-streaming-sortformer-4spk-v2-1"]
Language = Literal["ar", "de", "en", "es", "fr", "hi", "it", "ja", "ko", "pt", "vi", "zh"]
Voice = Literal["Aria", "Jason", "John", "Leo", "Sofia"]
# v2607 model card ordering. The Voice-Agent example still carries the OLD
# checkpoint ordering: using it silently assigns the wrong named speaker.
VOICES = {"Aria": 0, "Jason": 1, "John": 2, "Leo": 3, "Sofia": 4}
LANGUAGES = ("ar", "de", "en", "es", "fr", "hi", "it", "ja", "ko", "pt", "vi", "zh")


@dataclass(frozen=True)
class ModelSpec:
    repository: str
    revision: str
    filename: str
    sha256: str
    capability: str
    sample_rate_hz: int
    license: str = "NVIDIA-Open-Model-License"


MODELS = {
    PARAKEET: ModelSpec("nvidia/parakeet_realtime_eou_120m-v1", "a7e2b4629593dce0ec19f600e00e9904353fda2d",
                       "parakeet_realtime_eou_120m-v1.nemo", "6603a22a53b7c1a4bac4736cb24628fb568a7102ba931a28c799e2e72f109893", "asr-eou", 16000),
    MAGPIE: ModelSpec("nvidia/magpie_tts_multilingual_357m", "19806879b16d3f2ccf28fb112b1bcd16a3c7923e",
                     "magpie_tts_multilingual_357m.nemo", "ec675fa8c02b9c1d5382c5c2b5a6acec6492c1e8344866c07cf3892185d18953", "tts", 22050),
    SORTFORMER: ModelSpec("nvidia/diar_streaming_sortformer_4spk-v2.1", "fafaab5faa1617a0ca52d38dd3dc4bd636800d3d",
                         "diar_streaming_sortformer_4spk-v2.1.nemo", "8abd32832159c6ac1148c926b7276f35ba34582c444e559dce1f1253fea42ef8", "diarization", 16000),
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AudioFormat(Strict):
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate_hz: Literal[16000] = 16000
    channels: Literal[1] = 1


class StreamStart(Strict):
    type: Literal["session.start"] = "session.start"
    model: Literal["parakeet-realtime-eou-120m-v1", "diar-streaming-sortformer-4spk-v2-1"]
    audio: AudioFormat = Field(default_factory=AudioFormat)


class StreamControl(Strict):
    type: Literal["session.finish", "session.cancel", "session.reset"]


class SynthesisRequest(Strict):
    model: Literal["magpie-tts-multilingual-357m"] = MAGPIE
    text: str = Field(min_length=1, max_length=4096)
    language: Language = "en"
    voice: Voice = "Sofia"
    apply_text_normalization: bool = False

    @field_validator("text")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must contain non-whitespace characters")
        return value


def capabilities(model: str) -> dict:
    return {
        "schema": "fs2.voice/v1", "model": model, **asdict(MODELS[model]),
        "runtime_revision": SPEECH_REVISION, "adapter_revision": VOICE_AGENT_REVISION,
        "max_concurrent_sessions": 1, "snapshot_enabled": False,
        "languages": list(LANGUAGES) if model == MAGPIE else ["en"] if model == PARAKEET else [],
        "voices": list(VOICES) if model == MAGPIE else [],
        "streaming_mode": "phrase_incremental" if model == MAGPIE else "cache_aware",
        "max_text_characters": 4096 if model == MAGPIE else None,
        "max_session_seconds": 1800, "max_frame_bytes": 32000, "resume_supported": False,
        "reconnect_policy": "new session; reset clears all model state; never replay accepted audio",
        "preemption_policy": "terminal worker_lost; new session required",
    }
