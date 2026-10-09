"""Isolated candidate startup: paired CUDA/math proof, then real HTTP serving.

Use only the retained public Arabidopsis chloroplast cases. No production
configuration or weights are modified; original functions are restored only
inside this isolated process for paired comparisons.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/opt/fs2-evo2")
import torch
from evo2_deep.runtime import GenerationRequest
from evo2_h100 import H100Backend
from evo2_prefill import modal_fft_prefill_tiled
from evo2_serve import MemoryAwareHTTPServer
from vortex.model.engine import HyenaInferenceEngine


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def difference(actual, expected):
    actual, expected = actual.double(), expected.double()
    delta = (actual - expected).abs()
    return {
        "max_absolute_error": delta.max().item(),
        "max_relative_error": (delta / expected.abs().clamp_min(1e-6)).max().item(),
        "relative_l2_error": (torch.linalg.vector_norm(delta) / torch.linalg.vector_norm(expected).clamp_min(1e-12)).item(),
        "bitwise_equal": torch.equal(actual, expected),
    }


def state_proof(output):
    rows = []
    for device in range(torch.cuda.device_count()):
        with torch.cuda.device(device), torch.inference_mode():
            torch.manual_seed(180926)
            # The exact maximum-size state fits only before model weights are
            # resident. It proves the formerly failing FFT shape without
            # submitting an oversized model request or changing any API bound.
            for hidden, length, groups, state_size in (
                (32, 127, 4, 16), (256, 2048, 256, 16),
                (1024, 4096, 1024, 16), (8192, 8192, 8192, 16),
            ):
                x = torch.randn(1, hidden, length, dtype=torch.bfloat16, device=f"cuda:{device}")
                poles = -torch.rand(groups, state_size, 1, device=x.device)
                t = torch.arange(length, device=x.device)[None, None]
                frequency = torch.fft.fft(x.float(), n=2 * length)
                params = SimpleNamespace(state_dict={})
                args = dict(inference_params=params, x1v=x, L=length, poles=poles, t=t,
                            dims=(hidden, 0, 0, state_size, groups), layer_idx=2, X_s=frequency)
                torch.cuda.reset_peak_memory_stats(device)
                HyenaInferenceEngine.prefill_via_modal_fft(None, **args)
                torch.cuda.synchronize(device)
                baseline_peak = torch.cuda.max_memory_allocated(device)
                expected = params.state_dict.pop(2).clone()
                torch.cuda.reset_peak_memory_stats(device)
                modal_fft_prefill_tiled(None, **args)
                torch.cuda.synchronize(device)
                actual = params.state_dict.pop(2)
                metrics = difference(actual, expected)
                torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-5)
                rows.append({"device": device, "hidden": hidden, "prefix_length": length, "groups": groups,
                             "state_size": state_size, "baseline_peak_bytes": baseline_peak,
                             "tiled_peak_bytes": torch.cuda.max_memory_allocated(device), **metrics})
                del x, poles, t, frequency, expected, actual, args, params
            torch.cuda.empty_cache()
    write(output / "cuda-state-proof.json", rows)
    print(json.dumps({"event": "cuda-state-proof", "results": rows}), flush=True)


def model_proof(backend, cases, output):
    pairs = []
    for length in (256, 1024, 4096):
        case = next(case for case in cases if len(case["arguments"]["sequence"]) == length)
        for seed in (7, 23, 41):
            parameters = {**case["arguments"], "num_tokens": 8, "random_seed": seed}
            request = GenerationRequest.from_mapping(parameters)
            observations = []
            for mode in ("baseline-a", "baseline-b", "tiled"):
                for engine, original, tiled in backend._modal_fft_bindings:
                    engine.prefill_via_modal_fft = tiled if mode == "tiled" else original
                logits = []
                hook = backend.model.register_forward_hook(
                    lambda _module, _args, result: logits.append(result[0][:, -1, :].detach().float().cpu())
                )
                try:
                    result = backend.generate(request)
                finally:
                    hook.remove()
                tensor = torch.stack(logits)
                observations.append((mode, result, tensor))
                write(output / f"paired-p{length}-s{seed}-{mode}.json", {
                    "case_id": case["case_id"], "parameters": parameters, "response": result,
                    "last_position_logits": tensor.tolist(),
                })
            baseline_a, baseline_b, tiled = observations
            metrics = difference(tiled[2], baseline_b[2])
            baseline_variation = difference(baseline_b[2], baseline_a[2])
            identical_sequences = len({row[1]["sequence"] for row in observations}) == 1
            pairs.append({"prefix_length": length, "seed": seed, "output_tokens": 8,
                          "same_seeded_outputs": identical_sequences, "tiled_vs_baseline": metrics,
                          "baseline_repeat_variation": baseline_variation,
                          "elapsed_ms": {mode: result["elapsed_ms"] for mode, result, _ in observations}})
            write(output / "paired-model-proof.json", pairs)
            if not identical_sequences or not torch.allclose(tiled[2], baseline_b[2], rtol=1e-4, atol=1e-4):
                raise RuntimeError("paired logits/output mismatch; candidate remains unqualified")
    for engine, original, tiled in backend._modal_fft_bindings:
        engine.prefill_via_modal_fft = tiled


class ObservedBackend:
    def __init__(self, backend, output):
        self.backend, self.output, self.count = backend, output, 0
        self.active_requests = 0
        self.accounting_lock = threading.Lock()

    @property
    def ready(self):
        return self.backend.ready

    @property
    def identity(self):
        return self.backend.identity

    def generate(self, request):
        with self.accounting_lock:
            self.count += 1
            ordinal = self.count
            self.active_requests += 1
            active_at_start = self.active_requests
        for device in (0, 1):
            torch.cuda.reset_peak_memory_stats(device)
        started = time.perf_counter()
        started_unix = time.time()
        try:
            return self.backend.generate(request)
        finally:
            finished_unix = time.time()
            with self.accounting_lock:
                self.active_requests -= 1
            write(self.output / f"http-memory-{ordinal:04d}.json", {
                "sequence_sha256": hashlib.sha256(request.sequence.encode()).hexdigest(),
                "prefix_length": len(request.sequence), "num_tokens": request.num_tokens,
                "seed": request.random_seed, "wall_seconds": time.perf_counter() - started,
                "started_unix_seconds": started_unix, "finished_unix_seconds": finished_unix,
                "active_model_requests_at_start": active_at_start,
                "memory": [{"device": device, "peak_allocated_bytes": torch.cuda.max_memory_allocated(device),
                            "peak_reserved_bytes": torch.cuda.max_memory_reserved(device),
                            "allocated_bytes": torch.cuda.memory_allocated(device)} for device in (0, 1)],
            })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for directory in ("/tmp/cuda", "/tmp/triton", "/tmp/torch-extensions", "/tmp/torchinductor"):
        Path(directory).mkdir(exist_ok=True)
    state_proof(args.output)
    if args.state_only:
        return
    cases = json.loads(args.cases.read_text())["cases"]
    started = time.perf_counter()
    backend = H100Backend(Path("/model-cache/evo2_40b.pt"))
    write(args.output / "runtime-identity.json", {**backend.identity, "initial_load_wall_seconds": time.perf_counter()-started})
    model_proof(backend, cases, args.output)
    server = MemoryAwareHTTPServer(("127.0.0.1", 8001), ObservedBackend(backend, args.output))
    print("ISOLATED_EVO2_CANDIDATE_READY", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
