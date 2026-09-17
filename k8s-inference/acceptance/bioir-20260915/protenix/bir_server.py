#!/usr/bin/env python3
"""Evaluation-only BIR model under the pinned Protenix featurizer and dumper.

The HTTP/CLI bridge and validation stay unchanged. The BIR module replaces only
InferenceRunner's model construction/checkpoint conversion/forward. This is a
prototype, not a published BIR pipeline or a production promotion.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import torch


def batched(value):
    if isinstance(value, torch.Tensor):
        return value.unsqueeze(0)
    if isinstance(value, dict):
        return {key: batched(item) for key, item in value.items()}
    return value


def graph_state(model):
    """Record actual capture/replay state, not just an enabled flag."""
    return [
        {"module": name, "states": [
            {"state": str(getattr(state, "preparation_state", "unknown")),
             "calls": getattr(state, "num_prev_calls_by_input_key", None),
             "captured": getattr(state, "graph", None) is not None}
            for state in states.values()]}
        for name, module in model.named_modules()
        if (states := getattr(module, "graph_state_by_key", None)) is not None
    ]


def install_bir_backend() -> None:
    from runner.inference import InferenceRunner
    from protenix.model.protenix import update_input_feature_dict
    from protenix.utils.torch_utils import to_device
    from bionemo_ir.models.protenix import Protenix

    def initialize(runner):
        if runner.configs.model_name != "protenix-v2" or runner.configs.dtype != "bf16":
            raise ValueError("prototype binds exact Protenix v2 BF16 serving profile")
        torch.backends.cuda.matmul.allow_tf32 = bool(runner.configs.enable_tf32)
        runner.model = Protenix(include_load_weights=False).to(runner.device)

    def load_checkpoint(runner):
        path = Path(runner.configs.load_checkpoint_dir) / "protenix-v2.pt"
        state = torch.load(path, map_location="cpu", weights_only=False)["model"]
        state = {key.removeprefix("module."): value for key, value in state.items()}
        runner.model.load_weights(state)
        runner.model.eval()
        if os.environ.get("BIOIR_GRAPHS", "0") == "1":
            # Public BIR explicitly excludes Protenix trunk/confidence pairformers
            # from graph capture because their replay can produce NaNs.
            runner.model.optimize({"token_transformer": {"backend": "torch"}})
        torch.cuda.synchronize()
        print("BIOIR_BINDING " + json.dumps({
            "model": "protenix-v2", "checkpoint": str(path),
            "graphs": os.environ.get("BIOIR_GRAPHS", "0"),
            "torch": torch.__version__, "cuda": torch.version.cuda,
            "config": runner.model.config.model_dump(mode="json"),
        }), flush=True)

    @torch.inference_mode()
    def predict(runner, data):
        started = time.monotonic()
        features = to_device(data["input_feature_dict"], runner.device)
        features = update_input_feature_dict(features)
        batch = batched(features)
        torch.cuda.synchronize()
        forward_started = time.monotonic()
        prediction = runner.model(
            batch, recycling_steps=int(runner.configs.model.N_cycle) - 1,
            num_sampling_steps=int(runner.configs.sample_diffusion.N_step),
            diffusion_samples=int(runner.configs.sample_diffusion.N_sample),
            compact_output=True, return_full_data=True,
            # Native preprocessing already consumed the caller-seeded RNG.
            # None preserves that stream; reseeding a private generator here
            # changes the diffusion trajectory despite the same request seed.
            sampling_seed=None,
        )
        torch.cuda.synchronize()
        print("BIOIR_PHASES " + json.dumps({
            "feature_transfer_seconds": forward_started - started,
            "forward_seconds": time.monotonic() - forward_started,
            "cycles": int(runner.configs.model.N_cycle),
            "steps": int(runner.configs.sample_diffusion.N_step),
            "samples": int(runner.configs.sample_diffusion.N_sample),
            "seed": int(torch.initial_seed()),
            "sampling_rng": "caller-global-stream",
            "graph_state": graph_state(runner.model),
            "max_allocated_bytes": torch.cuda.max_memory_allocated(),
            "max_reserved_bytes": torch.cuda.max_memory_reserved(),
        }), flush=True)
        return prediction

    InferenceRunner.init_model = initialize
    InferenceRunner.load_checkpoint = load_checkpoint
    InferenceRunner.predict = predict


if __name__ == "__main__":
    install_bir_backend()
    from protenix_server import ProtenixBackend
    from scientific_server import serve

    serve(ProtenixBackend())
