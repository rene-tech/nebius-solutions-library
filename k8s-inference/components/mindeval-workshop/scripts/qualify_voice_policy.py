"""Qualify pinned CPU VAD on a supplied non-sensitive mono 16 kHz PCM16 WAV."""

import argparse
import hashlib
import json
import time
import wave
from pathlib import Path

from fs2_workshop.voice_policy import PARAKEET, SileroCPU, VoicePolicy

parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
parser.add_argument("--wav", required=True)
args = parser.parse_args()
started = time.monotonic()
model = SileroCPU(args.model)
load_ms = (time.monotonic() - started) * 1000
with wave.open(args.wav) as wav:
    assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
    pcm = wav.readframes(wav.getnframes())
policy = VoicePolicy(model, asr_model=PARAKEET)
audio = pcm + bytes(32000 * 2)  # Explicitly disclosed trailing silence for boundary qualification.
started = time.monotonic()
for offset in range(0, len(audio), 1024):
    policy.feed(audio[offset : offset + 1024])
elapsed = time.monotonic() - started
assert any(event["type"] == "voice_policy.speech_start" for event in policy.events)
assert any(event["type"] == "voice_policy.speech_end" for event in policy.events)
print(
    json.dumps(
        {
            "wav_sha256": hashlib.sha256(Path(args.wav).read_bytes()).hexdigest(),
            "input_audio_seconds": len(pcm) / 32000,
            "appended_silence_seconds": 2,
            "model_load_ms": load_ms,
            "cpu_inference_ms": elapsed * 1000,
            "processed_seconds": policy.samples / 16000,
            "real_time_factor": elapsed / (policy.samples / 16000),
            "policy": policy.metadata(),
        },
        indent=2,
    )
)
