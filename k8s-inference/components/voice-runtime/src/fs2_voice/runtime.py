"""Pinned upstream model calls; the serving layer owns transport and admission."""

import hashlib
import re
import threading
import time
from pathlib import Path

import numpy as np

from .contracts import MAGPIE, MODELS, PARAKEET, VOICES, SynthesisRequest


def phrases(text: str, limit: int = 100):
    """Bound first audio work while preserving every character in order.

    This is phrase-level incremental synthesis, not codec-token streaming.
    Punctuation is kept. Whitespace is only stripped at each phrase edge.
    """
    for sentence in re.split(r"(?<=[.!?;。！？；])\s*", text):
        sentence = sentence.strip()
        while sentence:
            end = len(sentence)
            if end > limit:
                split = sentence.rfind(" ", 1, limit + 1)
                end = split if split > 0 else limit
            chunk, sentence = sentence[:end].strip(), sentence[end:].strip()
            if chunk:
                yield chunk


class Runtime:
    def __init__(self, model: str, checkpoint_dir: str = "/opt/fs2-voice/weights"):
        if model not in MODELS:
            raise ValueError("unknown_model")
        self.model_id = model
        self.checkpoint_dir = Path(checkpoint_dir)
        self.model = None
        self.timings = {}
        self.frame_bytes = 2560 if model == PARAKEET else 15360
        self.pending = bytearray()
        self.text = ""
        self.segment = 0
        self.samples = 0
        self.frames = 0

    def load(self):
        started = time.monotonic()
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError("cuda_required")
        spec = MODELS[self.model_id]
        checkpoint = self.checkpoint_dir / spec.filename
        sha = hashlib.file_digest(checkpoint.open("rb"), "sha256").hexdigest()
        if sha != spec.sha256:
            raise RuntimeError("checkpoint_checksum_mismatch")
        self.timings["checkpoint_verify_seconds"] = time.monotonic() - started
        started = time.monotonic()
        if self.model_id == PARAKEET:
            from fs2_voice_upstream.streaming_asr import NemoStreamingASRService

            self.model = NemoStreamingASRService(str(checkpoint), att_context_size=[70, 1], device="cuda")
        elif self.model_id == MAGPIE:
            from nemo.collections.tts.models import MagpieTTSModel
            from omegaconf import OmegaConf, open_dict

            cfg = MagpieTTSModel.restore_from(str(checkpoint), return_config=True)
            with open_dict(cfg):
                cfg.codecmodel_path = str(self.checkpoint_dir / "nanocodec.nemo")
            self.model = MagpieTTSModel.restore_from(str(checkpoint), override_config_path=cfg, map_location="cuda")
            self.model.eval().cuda()
        else:
            from fs2_voice_upstream.streaming_diar import DiarizationConfig, NeMoStreamingDiarService

            class StrictDiar(NeMoStreamingDiarService):
                # Upstream helper catches GPU errors and returns stale output.
                # The service must fail the request instead. total_preds is only
                # output accumulation; speaker/FIFO history lives in streaming_state.
                def stream_step(self, **kwargs):
                    kwargs["total_preds"] = kwargs["total_preds"][:, :0, :]
                    with torch.inference_mode():
                        return self.diarizer.forward_streaming_step(**kwargs)

            self.model = StrictDiar(DiarizationConfig(), str(checkpoint))
        torch.cuda.synchronize()
        self.timings["model_load_seconds"] = time.monotonic() - started
        started = time.monotonic()
        if self.model_id == MAGPIE:
            list(self.synthesize(SynthesisRequest(text="Hello, how can I help?"), threading.Event()))
        else:
            self.feed(bytes(self.frame_bytes * 2))
            self.reset()
        torch.cuda.synchronize()
        self.timings["warmup_seconds"] = time.monotonic() - started

    def reset(self):
        if self.model_id != MAGPIE and self.model is not None:
            self.model.reset_state()
        self.pending.clear()
        self.text = ""
        self.segment = self.samples = self.frames = 0

    def _step(self, pcm: bytes):
        events = []
        if self.model_id == PARAKEET:
            result = self.model.transcribe(pcm)
            delta = result.text.replace("<EOU>", "").replace("<EOB>", "")
            self.text += delta
            if delta:
                events.append({"type": "transcript.partial", "segment": self.segment, "text": self.text.strip()})
            if result.is_final:
                events.append({"type": "transcript.final", "segment": self.segment, "text": self.text.strip()})
                for token, event, probability in (("<EOU>", "turn.eou", result.eou_prob),
                                                   ("<EOB>", "turn.eob", result.eob_prob)):
                    if token in result.text:
                        events.append({"type": event, "segment": self.segment, "source": "model_token",
                                       "probability": probability, "audio_offset_seconds": self.samples / 16000})
                self.segment += 1
                self.text = ""
        else:
            probabilities = self.model.diarize(pcm)
            if not np.isfinite(probabilities).all() or probabilities.ndim != 2 or probabilities.shape[1] != 4:
                raise RuntimeError("invalid_speaker_probabilities")
            events.append({"type": "speaker.activity", "start_seconds": self.frames * .08,
                           "frame_duration_seconds": .08, "speakers": ["speaker_0", "speaker_1", "speaker_2", "speaker_3"],
                           "probabilities": probabilities.tolist()})
            self.frames += len(probabilities)
            self.model.total_preds = self.model.total_preds[:, -6:, :]
        return events

    def feed(self, pcm: bytes):
        if not pcm or len(pcm) % 2 or len(pcm) > 32000:
            raise ValueError("invalid_pcm_frame")
        if self.samples + len(pcm) // 2 > 1800 * 16000:
            raise ValueError("session_audio_limit")
        self.samples += len(pcm) // 2
        self.pending.extend(pcm)
        events = []
        while len(self.pending) >= self.frame_bytes:
            chunk = bytes(self.pending[:self.frame_bytes])
            del self.pending[:self.frame_bytes]
            events.extend(self._step(chunk))
        return events

    def finish(self):
        events = []
        if self.pending:
            events.extend(self._step(bytes(self.pending).ljust(self.frame_bytes, b"\0")))
            self.pending.clear()
        if self.model_id == PARAKEET:
            # Flush the model's lookahead/EOU tail without billing synthetic silence.
            for _ in range(10):
                events.extend(self._step(bytes(self.frame_bytes)))
            if self.text.strip():
                events.append({"type": "transcript.final", "segment": self.segment, "text": self.text.strip(),
                               "reason": "session_finish"})
                self.text = ""
        return events

    def synthesize(self, request: SynthesisRequest, cancelled: threading.Event):
        import torch

        for phrase in phrases(request.text):
            if cancelled.is_set():
                return
            with torch.inference_mode():
                audio, lengths = self.model.do_tts(phrase, language=request.language,
                                                  apply_TN=request.apply_text_normalization,
                                                  speaker_index=VOICES[request.voice])
            if cancelled.is_set():
                return
            length = int(lengths.reshape(-1)[0])
            samples = audio.detach().reshape(-1)[:length].cpu().numpy()
            if not length or not np.isfinite(samples).all():
                raise RuntimeError("invalid_audio_output")
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            # Bound output events to <=100ms for transport/playout buffers.
            for offset in range(0, len(pcm), 4410):
                if cancelled.is_set():
                    return
                yield pcm[offset:offset + 4410]
