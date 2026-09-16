"""Small adapter over NVIDIA's pinned cache-aware RNNT streaming pipeline.

No encoder, decoder, feature extraction, or language-prompt implementation is
forked here. One loaded profile owns its streams; model-wide settings are fixed.
The first qualification lane deliberately admits one stream at a time.
"""

import time
from pathlib import Path
from typing import Any

from .contracts import MODELS, RuntimeProfile, SpeechOptions
from .framing import PCMFrame


class NeMoRuntime:
    def __init__(self, profile: RuntimeProfile, *, config_path: Path, cache_dir: str | None = None) -> None:
        self.profile = profile
        self.config_path = config_path
        self.cache_dir = cache_dir
        self.pipeline: Any = None
        self.timings: dict[str, float] = {}
        self._active: int | None = None
        self._next_stream = 0

    def load(self) -> None:
        if self.pipeline is not None:
            raise RuntimeError("runtime_already_loaded")
        start = time.monotonic()
        import torch
        from huggingface_hub import hf_hub_download
        from nemo.collections.asr.inference.factory.pipeline_builder import PipelineBuilder
        from omegaconf import OmegaConf

        if not torch.cuda.is_available():
            raise RuntimeError("GPU qualification requires a CUDA device; no silent CPU fallback")
        self.timings["runtime_import_seconds"] = time.monotonic() - start
        spec = MODELS[self.profile.model]
        start = time.monotonic()
        checkpoint = hf_hub_download(
            spec.repository, filename=spec.filename, revision=spec.revision, cache_dir=self.cache_dir,
        )
        self.timings["checkpoint_acquisition_seconds"] = time.monotonic() - start
        cfg = OmegaConf.load(self.config_path)
        cfg.asr.model_name = checkpoint
        cfg.asr.device = "cuda"
        cfg.asr.device_id = 0
        cfg.asr.compute_dtype = self.profile.precision
        cfg.asr.use_amp = False
        cfg.asr.use_cuda_graphs = self.profile.cuda_graphs
        cfg.asr.strip_lang_tags = self.profile.strip_language_tags
        cfg.asr.decoding.strategy = self.profile.decoding
        cfg.asr.decoding.beam.beam_size = self.profile.beam_size
        cfg.asr.decoding.greedy.preserve_frame_confidence = self.profile.confidence
        cfg.asr.decoding.beam.preserve_frame_confidence = self.profile.confidence
        cfg.streaming.att_context_size = [spec.left_context, self.profile.chunk_size_ms // 80 - 1]
        cfg.streaming.batch_size = 1
        cfg.streaming.num_slots = 1
        cfg.enable_itn = False
        cfg.enable_nmt = False
        cfg.return_tail_result = True
        cfg.lang = spec.default_language
        start = time.monotonic()
        self.pipeline = PipelineBuilder.build_pipeline(cfg)
        torch.cuda.synchronize()
        self.timings["model_load_seconds"] = time.monotonic() - start

    @property
    def frame_samples(self) -> int:
        if self.pipeline is None:
            raise RuntimeError("model_not_loaded")
        return round(self.pipeline.chunk_size_in_secs * self.pipeline.sample_rate)

    def begin(self, options: SpeechOptions) -> int:
        if self.pipeline is None:
            raise RuntimeError("model_not_loaded")
        self.profile.require_match(options)
        if self._active is not None:
            raise RuntimeError("runtime_busy")
        self._next_stream += 1
        self._active = self._next_stream
        return self._active

    def step(self, stream_id: int, frame: PCMFrame, options: SpeechOptions) -> Any:
        import numpy as np
        import torch
        from nemo.collections.asr.inference.streaming.framing.request import Frame
        from nemo.collections.asr.inference.streaming.framing.request_options import ASRRequestOptions

        if stream_id != self._active:
            raise RuntimeError("unknown_stream")
        self.profile.require_match(options)
        samples = torch.from_numpy(np.frombuffer(frame.pcm, dtype="<i2").astype(np.float32) / 32768.0)
        request = Frame(
            samples=samples,
            stream_id=stream_id,
            is_first=frame.first,
            is_last=frame.last,
            length=frame.valid_samples,
            options=ASRRequestOptions(
                language_code=options.resolved_language,
                stop_history_eou=options.stop_history_eou_ms,
                asr_output_granularity=options.output_granularity,
            ),
        )
        with torch.inference_mode():
            result = self.pipeline.transcribe_step([request])[0]
        return result

    def close(self, stream_id: int) -> None:
        """Free encoder/audio/decoder state on EOS, cancellation and failure.

        Called only between GPU steps, never while a kernel/step is in flight.
        No other stream exists in this single-admission qualification profile.
        """
        if stream_id != self._active:
            return
        try:
            bufferer = self.pipeline.bufferer
            slot = bufferer.streamidx2slotidx.get(stream_id)
            if slot is not None:
                bufferer.reset_slots([slot])
                bufferer.free_slots([slot])
            context = self.pipeline.context_manager
            if stream_id in context.streamidx2slotidx:
                context.reset_slots([stream_id], [True])
            self.pipeline.close_session()
        finally:
            self._active = None
