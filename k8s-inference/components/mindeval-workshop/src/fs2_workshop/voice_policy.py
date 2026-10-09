"""Opt-in CPU voice boundaries; never generates speech or suppresses human text.

Silero ONNX adapter follows v6.2 utils_vad.OnnxWrapper (MIT).
English matching adapts NeMo Voice-Agent a01fa68f clean_text/is_backchannel
(Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES, Apache-2.0).
See VOICE_POLICY.md and voice_policy_NOTICE for exact source and limitations.
"""

import hashlib
from pathlib import Path

import numpy as np
import onnxruntime as ort

SILERO_REVISION = "be95df9152c0d7618fa1edfeb296fc3dae32376f"
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
NEMO_REVISION = "a01fa68f0907a52cca1b07115e0e37c68ca580e5"
PARAKEET = "parakeet-realtime-eou-120m-v1"
# Pinned upstream examples/generic_voice_agent/server/backchannel_phrases.yaml.
BACKCHANNEL_PHRASES = """absolutely|ah|all right|alright|but yeah|cool|definitely|exactly|go ahead|good|great|
great thanks|ha ha|hmm|humm|huh|i know|i know right|i see|indeed|interesting|mhmm|mhmm mhmm|mhmm right|
mhmm yeah|mhmm yes|mm hmm|mmhmm|nice|of course|oh|oh dear|oh man|oh okay|oh wow|oh yes|ok|ok thanks|
okay|okay okay|okay thanks|perfect|really|right|right exactly|right right|right yeah|so yeah|sounds good|
sure|sure thing|thank you|thanks|that's awesome|thats right|thats true|true|uh huh|uh-huh|uh-huh yeah|
uhhuh|uhhuh okay|um-humm|well|what|wow|yeah|yeah i know|yeah i see|yeah mhmm|yeah okay|yeah right|
yeah uh-huh|yeah yeah|yep|yes|yes please|yes yes"""


def clean_text(text):
    """NeMo's English phrase normalization, without Pipecat frame dependencies."""
    for marker in ("<EOU>", "<EOB>"):
        if text.endswith(marker):
            text = text[: -len(marker)].strip()
    text = "".join(c for c in text.lower() if c in "abcdefghijklmnopqrstuvwxyz'" or c.isspace())
    return " ".join(text.split()).strip()


PHRASES = frozenset(clean_text(text.strip()) for text in BACKCHANNEL_PHRASES.split("|"))


class SileroCPU:
    """One immutable ONNX session per API process; recurrent state is not shared."""

    def __init__(self, path):
        model = Path(path).read_bytes()
        if hashlib.sha256(model).hexdigest() != SILERO_SHA256:
            raise ValueError("Silero checkpoint checksum mismatch")
        options = ort.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(model, options, providers=["CPUExecutionProvider"])

    def probability(self, samples, state, context):
        # Pinned ONNX contract: 512 new 16 kHz samples + 64 context samples.
        audio = np.concatenate((context, samples.reshape(1, 512)), axis=1)
        probability, state = self.session.run(
            None, {"input": audio, "state": state, "sr": np.array(16000, dtype=np.int64)}
        )
        return float(probability.item()), state, audio[:, -64:]


class VoicePolicy:
    """Per-microphone state; EOU/model EOB and VAD silence stay distinguishable."""

    def __init__(self, model, *, asr_model, language="en"):
        self.model, self.asr_model, self.language = model, asr_model, language
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)
        self.pending = bytearray()
        self.samples = self.speech_frames = self.silence_frames = 0
        self.speaking = self.finished = False
        self.events, self.candidates = [], set()

    def event(self, kind, source, **extra):
        result = {
            "type": "voice_policy." + kind,
            "source": source,
            "audio_offset_seconds": self.samples / 16000,
            "automatic_speech": False,
            **extra,
        }
        if len(self.events) < 256:
            self.events.append(result)
        return result

    def feed(self, pcm):
        if not pcm or len(pcm) % 2 or len(pcm) > 32000:
            raise ValueError("Voice policy requires bounded mono PCM16 frames")
        if self.finished:
            return []
        self.pending.extend(pcm)
        emitted = []
        while len(self.pending) >= 1024:
            window = np.frombuffer(bytes(self.pending[:1024]), dtype="<i2").astype(np.float32) / 32768
            del self.pending[:1024]
            probability, self.state, self.context = self.model.probability(window, self.state, self.context)
            self.samples += 512
            if not np.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("Invalid Silero speech probability")
            self.speech_frames = self.speech_frames + 1 if probability >= 0.6 else 0
            if not self.speaking and self.speech_frames >= 4:  # 128 ms, >= NeMo's 100 ms start.
                self.speaking = True
                emitted.append(self.event("speech_start", "silero_vad", probability=probability))
            if self.speaking:
                self.silence_frames = self.silence_frames + 1 if probability < 0.45 else 0
                if self.silence_frames >= 38:  # 1216 ms, >= NeMo's 1.2 s fallback.
                    self.finished = True
                    emitted.append(self.event("speech_end", "silero_vad_silence", model_eou=False))
                    break
        return emitted

    def observe(self, event):
        """Only authenticated upstream events can supply genuine model EOU/EOB."""
        kind, emitted = event.get("type"), []
        model_token = self.asr_model == PARAKEET and event.get("source") == "model_token"
        if model_token and kind == "turn.eou" and not self.finished:
            self.finished = True
            emitted.append(self.event("speech_end", "parakeet_model_eou", model_eou=True))
        if model_token and kind == "turn.eob":
            emitted.append(
                self.event(
                    "backchannel_candidate",
                    "parakeet_model_eob",
                    model_eob=True,
                    segment=event.get("segment"),
                    suppressed=False,
                )
            )
        if kind == "transcript.final" and self.language == "en":
            normalized = clean_text(str(event.get("text", "")))
            key = (event.get("segment", event.get("sequence")), normalized)
            if normalized in PHRASES and key not in self.candidates:
                self.candidates.add(key)
                emitted.append(
                    self.event(
                        "backchannel_candidate", "nemo_phrase_rule", model_eob=False, segment=key[0], suppressed=False
                    )
                )
        return emitted

    def metadata(self):
        return {
            "mode": "automatic_opt_in",
            "silero_revision": SILERO_REVISION,
            "silero_sha256": SILERO_SHA256,
            "nemo_logic_revision": NEMO_REVISION,
            "execution_provider": "CPUExecutionProvider",
            "events": self.events,
            "automatic_backchannel_speech": False,
            "text_suppression": False,
        }
