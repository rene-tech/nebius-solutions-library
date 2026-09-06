#!/usr/bin/env python3
"""Isolated ESMFold2 CUDA RAM checkpoint/restore experiment with variable inputs.

This retains the process and host RAM. It does not create a disk checkpoint or
claim Kubernetes can release/reassign a pod's GPU allocation after offload.
The existing image and mounted model artifacts must match the production pins.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

PREFIX = "FS2_PROBE "
SEQUENCES = (
    "MKTVRQERLKSIVRILERSKEPVSGAQLAEELSVSRQVIVQDIAYLRSLGYNIVATPRGYVLAGG",
    "MKTIIALSYIFCLVFADYKDDDDKGGGGSGGGGS",
)


def emit(value: dict) -> None:
    print(PREFIX + json.dumps(value), flush=True)


def worker() -> None:
    # The checkpoint helper is a sibling process in this isolated test pod.
    # Permit that debugger relationship without changing any host policy.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(0x59616D61, ctypes.c_ulong(-1), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_PTRACER for isolated CUDA probe")
    os.environ["ESMCFOLD_CCD_PATH"] = "/databases/esmfold2/ccd.pkl"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    started = time.monotonic()
    import torch
    from esm.models.esmc import EsmcModel
    from esm.models.esmfold2 import ESMFold2InputBuilder, EsmFold2Model
    from esm.utils.structure.input_builder import deserialize_structure_prediction_input

    model = EsmFold2Model.from_pretrained("/models/esmfold2", load_esmc=False, device="cuda").eval()
    model.esmc = EsmcModel.from_pretrained(
        "/models/esmc-6b", device="cuda", attn_implementation="flash_attention_2"
    )
    model.set_esmc_precision("bf16")
    torch.cuda.synchronize()
    emit({
        "event": "ready",
        "pid": os.getpid(),
        "load_seconds": time.monotonic() - started,
        "torch_version": torch.__version__,
        "cuda_version": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0),
        "compute_capability": list(torch.cuda.get_device_capability(0)),
        "allocated_bytes": torch.cuda.memory_allocated(),
        "reserved_bytes": torch.cuda.memory_reserved(),
    })
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("stop"):
            return
        if request.get("hash_state"):
            started = time.monotonic()
            digest = hashlib.sha256()
            count = 0
            total_bytes = 0
            for name, tensor in sorted(model.state_dict().items()):
                identity = json.dumps([name, str(tensor.dtype), list(tensor.shape)]).encode()
                digest.update(identity)
                data = tensor.detach().contiguous().view(torch.uint8).cpu().numpy()
                digest.update(memoryview(data))
                total_bytes += data.nbytes
                count += 1
            emit({
                "event": "model_state", "sha256": digest.hexdigest(),
                "tensor_count": count, "bytes": total_bytes,
                "seconds": time.monotonic() - started,
            })
            continue
        sequence = request["sequence"]
        prepared = deserialize_structure_prediction_input({
            "sequences": [{"type": "protein", "id": "A", "sequence": sequence, "msa": None}]
        })
        started = time.monotonic()
        result = ESMFold2InputBuilder().fold(
            model, prepared, num_loops=1, num_sampling_steps=4,
            num_diffusion_samples=1, seed=42, lm_dropout=0.0, complex_id="fs2-checkpoint-probe",
        )
        torch.cuda.synchronize()
        cif = result.complex.to_mmcif()
        plddt = float(result.plddt.detach().float().mean().cpu())
        atoms = len(re.findall(r"^(?:ATOM|HETATM)\s", cif, re.MULTILINE))
        if not math.isfinite(plddt) or not 0 <= plddt <= 1 or atoms < len(sequence) * 3:
            raise ValueError("restored ESMFold2 did not return a bounded protein structure")
        output_path = Path("/tmp") / (request["request_id"] + ".cif")
        output_path.write_text(cif)
        emit({
            "event": "result", "request_id": request["request_id"],
            "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
            "sequence_length": len(sequence), "atoms": atoms, "plddt_mean": plddt,
            "cif_sha256": hashlib.sha256(cif.encode()).hexdigest(),
            "cif_bytes": len(cif.encode()), "output": str(output_path),
            "inference_seconds": time.monotonic() - started,
        })


def receive(process: subprocess.Popen) -> dict:
    for line in process.stdout:
        if line.startswith(PREFIX):
            return json.loads(line[len(PREFIX):])
    raise RuntimeError(f"worker exited without a response: {process.poll()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--checkpoint-binary", type=Path, default=Path("/tmp/cuda-checkpoint"))
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.worker:
        worker()
        return
    receipt = {
        "schema": "fs2-serve.nebius.ai/esmfold2-cuda-ram-checkpoint-probe/v1",
        "mechanism": "CUDA process suspend to host RAM and resume; same process and GPU allocation",
        "disk_checkpoint": False,
        "production_enabled": False,
        "image_digest": "sha256:b372dd7e34e464680a82456ca31b403b0ac0d0851511930d471b67041adbbde3",
        "cuda_checkpoint_commit": "00d5cce84c628088d6caa203fc4af40c1538b6f7",
        "cuda_checkpoint_sha256": hashlib.sha256(args.checkpoint_binary.read_bytes()).hexdigest(),
        "driver_inventory": subprocess.check_output([
            "nvidia-smi", "--query-gpu=name,driver_version,compute_cap,memory.total", "--format=csv,noheader"
        ], text=True).strip(),
        "runs": [], "status": "running",
    }
    process = subprocess.Popen(
        [sys.executable, "-u", __file__, "--worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        receipt["ready"] = receive(process)
        baseline = [[], []]
        for repetition in range(1, args.repetitions + 1):
            for index, sequence in enumerate(SEQUENCES):
                process.stdin.write(json.dumps({
                    "request_id": f"baseline-{repetition}-{index}", "sequence": sequence
                }) + "\n")
                process.stdin.flush()
                baseline[index].append(receive(process))
        receipt["baseline"] = baseline
        for repetition in range(1, args.repetitions + 1):
            row = {"repetition": repetition}
            receipt["runs"].append(row)
            process.stdin.write(json.dumps({"hash_state": True}) + "\n")
            process.stdin.flush()
            row["state_before"] = receive(process)
            for action in ("lock", "checkpoint", "restore", "unlock"):
                command = [str(args.checkpoint_binary), "--action", action, "--pid", str(process.pid)]
                if action == "lock":
                    command.extend(("--timeout", "10000"))
                started = time.monotonic()
                completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
                row[action] = {
                    "seconds": time.monotonic() - started, "returncode": completed.returncode,
                    "stdout": completed.stdout, "stderr": completed.stderr,
                }
                if completed.returncode:
                    raise RuntimeError(f"CUDA {action} failed: {completed.stderr or completed.stdout}")
                if action == "checkpoint":
                    row["checkpointed_state"] = subprocess.check_output(
                        [str(args.checkpoint_binary), "--get-state", "--pid", str(process.pid)], text=True
                    ).strip()
                    row["offloaded_gpu_memory"] = subprocess.check_output([
                        "nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"
                    ], text=True).strip()
            process.stdin.write(json.dumps({"hash_state": True}) + "\n")
            process.stdin.flush()
            row["state_after"] = receive(process)
            row["model_state_byte_exact"] = row["state_before"]["sha256"] == row["state_after"]["sha256"]
            index = (repetition - 1) % len(SEQUENCES)
            process.stdin.write(json.dumps({
                "request_id": f"restored-{repetition}", "sequence": SEQUENCES[index]
            }) + "\n")
            process.stdin.flush()
            result = receive(process)
            row["result"] = result
            row["matches_baseline_structure"] = (
                result["sequence_sha256"] == baseline[index][0]["sequence_sha256"]
                and result["atoms"] == baseline[index][0]["atoms"]
            )
            row["ordinary_plddt_range"] = [
                min(item["plddt_mean"] for item in baseline[index]),
                max(item["plddt_mean"] for item in baseline[index]),
            ]
            row["confidence_validation"] = "finite-in-range-0-to-1; same production structural qualification"
            if not row["matches_baseline_structure"] or not row["model_state_byte_exact"]:
                raise ValueError("restored state or request-dependent structure differs")
        receipt["status"] = "passed"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
    finally:
        process.kill()
        process.wait(timeout=30)
        print(json.dumps(receipt, indent=2), flush=True)
    if receipt["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
