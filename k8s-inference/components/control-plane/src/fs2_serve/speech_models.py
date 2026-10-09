"""Reviewed speech wire contracts, not catalog records or release approval.

A derivative can reuse English request options without becoming the upstream
model. Its catalog, artifact, runtime variant and signed route stay distinct.
Registering a name here alone neither loads an App nor grants access to it.
"""

from types import MappingProxyType

MEDICAL_NEMOTRON = "nemotron-speech-en-medical-0-6b"
MEDICAL_NEMOTRON_VARIANT = "nemotron-speech-en-medical-0-6b-nemo-cuda-v1"
SPEECH_SCHEMA_BASES = MappingProxyType({
    "nemotron-speech-en-0-6b": "nemotron-speech-en-0-6b",
    "nemotron-speech-multilingual-0-6b": "nemotron-speech-multilingual-0-6b",
    MEDICAL_NEMOTRON: "nemotron-speech-en-0-6b",
})
SPEECH_MODELS = frozenset(SPEECH_SCHEMA_BASES)
LIVE_SPEECH_PATHS = MappingProxyType({
    **{model: "/v1/audio/stream" for model in SPEECH_MODELS},
    "parakeet-realtime-eou-120m-v1": "/v1/voice/stream",
    "diar-streaming-sortformer-4spk-v2-1": "/v1/voice/stream",
})
