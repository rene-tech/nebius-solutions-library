#!/usr/bin/env python3
"""Offline, artifact-explicit ESMFold2 and ESMFold2-Fast runtime boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from result_contract import finite_metric, write_confidence_envelope
from runtime_localization import (
    RuntimeArtifactExpectation,
    validate_runtime_localization,
)


CCD_BYTES = 417_306_584
ESMFOLD2_CONTENT_SHA256 = "136a3580c01cc055ae5a1278bae056e5150a5441ddb89dfbafb9f4e88d763a0c"
ESMFOLD2_FAST_CONTENT_SHA256 = "19ceaffb5860acf160ea199599fb719b0566519e4cc2fa7a7aa5ef547942ad63"
ESMC_CONTENT_SHA256 = "8f21da30919b3e0d7af9ec6c4b9879542234d77d42ce061fef029397a4d39758"
CCD_CONTENT_SHA256 = "b1c2fe19204c57f7a7cca6ab4cb0cb420b99312fff424ef2e405fc8234b7616e"
VARIANT_ID = "biohub-v3-4-0"


def _absolute_file(path: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise SystemExit(f"{label} must be an existing absolute file: {candidate}")
    return candidate


def _absolute_dir(path: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise SystemExit(f"{label} must be an existing absolute directory: {candidate}")
    return candidate


def _load_request(path: Path):
    from esm.utils.structure.input_builder import deserialize_structure_prediction_input

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not document.get("sequences"):
        raise SystemExit("ESMFold2 request must contain a non-empty sequences array")
    return deserialize_structure_prediction_input(document)


def _validate_runtime_localization_args(
    command: str, args: argparse.Namespace
) -> dict[str, object]:
    if command != "fold":
        raise SystemExit(f"{command} does not consume runtime artifacts")
    if args.variant == "esmfold2":
        model_artifact = RuntimeArtifactExpectation(
            "esmfold2-trunk", "/models/esmfold2", ESMFOLD2_CONTENT_SHA256
        )
    elif args.variant == "esmfold2-fast":
        model_artifact = RuntimeArtifactExpectation(
            "esmfold2-fast-trunk",
            "/models/esmfold2-fast",
            ESMFOLD2_FAST_CONTENT_SHA256,
        )
    else:
        raise SystemExit("variant must identify the exact ESMFold2 runtime")
    return validate_runtime_localization(
        args.runtime_localization_marker,
        model_id=args.variant,
        variant_id=VARIANT_ID,
        stage_id="fold",
        artifacts=(
            model_artifact,
            RuntimeArtifactExpectation("esmc-6b", "/models/esmc-6b", ESMC_CONTENT_SHA256),
            RuntimeArtifactExpectation(
                "esmfold2-ccd", "/databases/esmfold2", CCD_CONTENT_SHA256
            ),
        ),
    )


def _prepare(args: argparse.Namespace) -> None:
    from esm.utils.structure.input_builder import (
        deserialize_structure_prediction_input,
        serialize_structure_prediction_input,
    )

    input_path = _absolute_file(args.input, "input")
    if args.sequence:
        manifest = json.loads(input_path.read_text(encoding="utf-8"))
        chain: dict[str, object] = {
            "type": "protein",
            "id": "A",
            "sequence": args.sequence,
            "msa": None,
        }
        if args.mode == "precomputed-msa":
            msa_sequences = manifest.get("msa_sequences") if isinstance(manifest, dict) else None
            if not isinstance(msa_sequences, list) or not msa_sequences:
                raise SystemExit("precomputed-msa mode requires msa_sequences in input-manifest")
            chain["msa"] = {"sequences": msa_sequences}
        request_document = {"sequences": [chain]}
        request = deserialize_structure_prediction_input(request_document)
    else:
        request = _load_request(input_path)
    output = Path(args.output)
    if not output.is_absolute():
        raise SystemExit("output must be an absolute path")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(serialize_structure_prediction_input(request), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fold(args: argparse.Namespace) -> None:
    import torch

    _validate_runtime_localization_args("fold", args)
    if not torch.cuda.is_available():
        raise SystemExit("fold requires a CUDA GPU")
    capability = tuple(torch.cuda.get_device_capability(0))
    if args.hardware_mode == "h100" and capability != (9, 0):
        raise SystemExit(f"H100 readiness mode requires compute capability 9.0, got {capability}")
    runtime_id = os.environ.get("FS2_RUNTIME_ID", "")
    if args.variant and args.variant != runtime_id:
        raise SystemExit(f"variant {args.variant!r} does not match image runtime {runtime_id!r}")
    if args.num_loops < 1 or args.num_sampling_steps < 1:
        raise SystemExit("num-loops and num-sampling-steps must be positive")

    model_dir = _absolute_dir(args.model_dir, "model-dir")
    esmc_dir = _absolute_dir(args.esmc_dir, "esmc-dir")
    ccd_path = _absolute_file(args.ccd_path, "ccd-path")
    if ccd_path.stat().st_size != CCD_BYTES:
        raise SystemExit("ESMFold2 CCD does not match the exact locked byte size")
    os.environ.update(
        {
            "ESMCFOLD_CCD_PATH": str(ccd_path),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    request = _load_request(_absolute_file(args.request, "request"))
    output = Path(args.output) if args.output else Path(args.output_dir) / f"{args.complex_id}.cif"
    if not output.is_absolute():
        raise SystemExit("output or output-dir must resolve to an absolute path")
    output.parent.mkdir(parents=True, exist_ok=True)

    # The reviewed H100 lane may use its SM90 FlashAttention wheel. Every
    # portability-only lane uses framework SDPA so Blackwell never receives the
    # known sm80/sm90-only binary.
    attention = "flash_attention_2" if args.hardware_mode == "h100" else "sdpa"
    # ESM's conformer module resolves ESMCFOLD_CCD_PATH at import time, so all
    # ESMFold2 imports must remain below the explicit environment binding.
    from esm.models.esmc import EsmcModel
    from esm.models.esmfold2 import ESMFold2InputBuilder, EsmFold2Model
    import esm.models.esmfold2.layers as esmfold2_layers

    if args.hardware_mode != "h100":
        esmfold2_layers.FLASH_ATTN_AVAILABLE = False
    model = EsmFold2Model.from_pretrained(
        model_dir, load_esmc=False, device="cuda"
    ).eval()
    model.esmc = EsmcModel.from_pretrained(
        esmc_dir, device="cuda", attn_implementation=attention
    )
    model.set_esmc_precision(args.esmc_precision)
    result = ESMFold2InputBuilder().fold(
        model,
        request,
        num_loops=1 if args.smoke else args.num_loops,
        num_sampling_steps=2 if args.smoke else args.num_sampling_steps,
        num_diffusion_samples=1,
        seed=args.seed,
        lm_dropout=0.0,
        complex_id=args.complex_id,
    )
    output.write_text(result.complex.to_mmcif(), encoding="utf-8")
    if result.plddt is None:
        raise SystemExit("ESMFold2 produced no bounded pLDDT summary")
    metrics: dict[str, float] = {
        "plddt_mean": finite_metric(
            "plddt_mean",
            float(result.plddt.detach().float().mean().cpu()),
            0.0,
            1.0,
        )
    }
    if result.ptm is not None:
        metrics["ptm"] = finite_metric("ptm", float(result.ptm), 0.0, 1.0)
    if result.iptm is not None:
        metrics["iptm"] = finite_metric("iptm", float(result.iptm), 0.0, 1.0)
    confidence_path = output.parent / "confidence.json"
    confidence = write_confidence_envelope(
        output.parent,
        runtime_id=runtime_id,
        model_revision=os.environ.get("FS2_MODEL_REVISION", ""),
        seeds=[args.seed],
        samples_per_seed=1,
        results=[
            {
                "seed": args.seed,
                "sample_index": 0,
                "structure": output,
                "summary": None,
                "metrics": metrics,
            }
        ],
    )
    evidence = {
        "schema": "fs2.nebius.ai/esmfold2-semantic-smoke/v1",
        "runtime_id": runtime_id,
        "model_revision": os.environ.get("FS2_MODEL_REVISION"),
        "hardware_mode": args.hardware_mode,
        "device_name": torch.cuda.get_device_name(0),
        "compute_capability": list(capability),
        "attention": attention,
        "smoke_profile": args.smoke,
        "output": str(output),
        "output_bytes": output.stat().st_size,
        "confidence_output": str(confidence_path),
        "plddt_mean": metrics["plddt_mean"],
        "ptm": metrics.get("ptm"),
        "iptm": metrics.get("iptm"),
        "status": "passed",
    }
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-input")
    prepare_input = prepare.add_mutually_exclusive_group(required=True)
    prepare_input.add_argument("--input", dest="input")
    prepare_input.add_argument("--input-manifest", dest="input")
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--sequence")
    prepare.add_argument("--mode", choices=("single-sequence", "precomputed-msa"), default="single-sequence")
    prepare.add_argument("--seed", type=int, default=0)
    prepare.set_defaults(handler=_prepare)

    fold = subparsers.add_parser("fold")
    fold_input = fold.add_mutually_exclusive_group(required=True)
    fold_input.add_argument("--request", dest="request")
    fold_input.add_argument("--input", dest="request")
    fold_output = fold.add_mutually_exclusive_group(required=True)
    fold_output.add_argument("--output")
    fold_output.add_argument("--output-dir")
    fold.add_argument("--model-dir", default=os.environ.get("FS2_MODEL_DIR", ""))
    fold.add_argument("--esmc-dir", default=os.environ.get("FS2_ESMC_MODEL_DIR", ""))
    fold.add_argument("--ccd-path", default=os.environ.get("ESMCFOLD_CCD_PATH", ""))
    fold.add_argument("--hardware-mode", choices=("h100", "portability"), default="h100")
    fold.add_argument("--esmc-precision", choices=("bf16", "fp32"), default="bf16")
    fold.add_argument("--num-loops", type=int, default=20)
    fold.add_argument("--num-sampling-steps", type=int, default=200)
    fold.add_argument("--smoke", action="store_true", help="use the explicit 1-loop/2-step smoke profile")
    fold.add_argument("--seed", type=int, default=0)
    fold.add_argument("--complex-id", default="fs2-smoke")
    fold.add_argument("--variant", choices=("esmfold2", "esmfold2-fast"), required=True)
    fold.add_argument("--runtime-localization-marker", required=True)
    fold.set_defaults(handler=_fold)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
