#!/usr/bin/env python3
"""Promote completed RF pairs only after original result and geometry checks."""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile

from report_protenix_pairs import project


def restore_measurement(path):
    raw = path.read_bytes()
    text = "\n".join(line.partition(" ")[2] for line in raw.decode().splitlines())
    value, _ = json.JSONDecoder().raw_decode(text.lstrip())
    if value.get("action") != "restore" or value.get("status") != "passed":
        raise ValueError("actual checkpoint restore success was not retained")
    if '"mechanism": "cuda-criu-restored"' not in text:
        raise ValueError("snapshot supervisor did not confirm actual restore")
    timings = {}
    for row in value["records"]:
        if row["returncode"] != 0:
            raise ValueError("a checkpoint restore subprocess failed")
        command = row["command"]
        phase = (
            command[command.index("--action") + 1] if "--action" in command else "criu"
        )
        timings[phase] = timings.get(phase, 0) + row["seconds"]
    if not {"criu", "restore", "unlock"}.issubset(timings):
        raise ValueError("CPU, CUDA and unlock completion are all required")
    return {
        "criu_seconds": timings["criu"],
        "cuda_restore_seconds": timings["restore"],
        "cuda_unlock_seconds": timings["unlock"],
        "log_sha256": hashlib.sha256(raw).hexdigest(),
    }


def original_result(archive):
    with tarfile.open(archive) as bundle:
        stream = bundle.extractfile("./result.json")
        if stream is None:
            raise ValueError("original RF result envelope is absent")
        value = json.load(stream)
    if value["model_id"] != "rfdiffusion" or value["status"] != "succeeded":
        raise ValueError("original RF result did not succeed")
    if (
        not value["accelerator"]["cuda_execution_confirmed"]
        or len(value["designs"]) != 1
    ):
        raise ValueError("original RF result must contain one verified GPU design")
    design, request = value["designs"][0], value["request"]
    expected_length = int(request["contigs"][0].split("-")[0])
    if design["residue_count"] != expected_length or design["seed"] != request["seed"]:
        raise ValueError("RF design differs from original length/seed")
    if request["diffuser_T"] != 50 or request["num_designs"] != 1:
        raise ValueError("RF comparison changed diffusion steps or design count")
    return {
        "residues": design["residue_count"],
        "seed": design["seed"],
        "diffuser_T": request["diffuser_T"],
        "pdb_sha256": design["pdb"]["sha256"],
        "native_sampler_ready_seconds": value["upstream"]["model_ready_seconds"],
        "full_wrapper_seconds": value["total_seconds"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("receipt", "manifest", "config", "report", "bundle"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--entrypoint-configmap", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    config = json.loads(args.config.read_bytes())
    manifest = json.loads(args.manifest.read_bytes())
    receipt = json.loads(args.receipt.read_bytes())
    report = project(receipt, manifest)
    report.update(model_id="rfdiffusion", stage_id="inference")
    report["parameters"] = {
        "diffuser_T": 50,
        "num_designs": 1,
        "precision": "unchanged native checkpoint/runtime",
    }
    cases_by_input = {}
    for row in receipt["runs"]:
        checks = []
        for name in ("case-76", "case-96"):
            archive = args.receipt.parent / (row["pod"] + "-" + name + "-outputs.tar")
            result = original_result(archive)
            checks.append(result)
            cases_by_input.setdefault((result["residues"], result["seed"]), []).append(
                result["pdb_sha256"]
            )
        projected = next(
            item for item in report["runs"] if item["pod_uid"] == row["pod_uid"]
        )
        projected["original_result_checks"] = checks
        if row["mode"] == "restore":
            projected["actual_restore"] = restore_measurement(
                args.receipt.parent / (row["pod"] + "-lifecycle.log")
            )
    if set(cases_by_input) != {(76, 8100), (96, 8200)}:
        raise ValueError(
            "both original distinct input length/seed combinations are required"
        )
    if any(
        len(digests) != 6 or len(set(digests)) != 1
        for digests in cases_by_input.values()
    ):
        raise ValueError(
            "normal and restored RF coordinates are not byte-identical across all pairs"
        )
    report["inputs"] = [
        {
            "residues": count,
            "seed": seed,
            "pdb_sha256": digests[0],
            "normal_restore_coordinate_equality": "6/6 byte-identical",
        }
        for (count, seed), digests in sorted(cases_by_input.items())
    ]
    report["ready_boundary"] = (
        "Input-free checkpoint/model on CUDA; each request still creates its native Sampler, contigs and diffusion tables"
    )
    report["qualification_scope"] = (
        "Exact model-only worker and unchanged original CLI/output validators, two original length/seed requests. The paired worker uses the frozen native CLI proxy; the production metadata-only outer CLI and one-shot renderer have separate acceptance."
    )
    report["clock_notes"].append(
        "Historical 34.87s RF initialization included a request's Sampler; it is not comparable to this narrower immutable-model loader boundary."
    )
    report["clock_notes"].append(
        "The third normal Pod waited behind another task's GPU capture (423.420s Pod-to-ready); its container-start clock excludes this scheduling delay. Existing image and shared-filesystem caches were retained, with no host cache eviction."
    )
    report["clock_notes"].append(
        "The first restore repetition's initial observer attempt hit a post-health Pod-status race before running either input. Its failed harness receipt is retained privately; the successful fresh-Pod retry is the measured repetition."
    )
    report["bundle"].update(
        id=config["bundle_id"],
        subpath=config["bundle_path"],
        captured_runtime_path="/checkpoints/" + config["bundle_path"],
    )
    report["private_receipt_sha256"] = hashlib.sha256(
        args.receipt.read_bytes()
    ).hexdigest()
    source_root = root / "models/scientific-snapshot"
    special = {
        "sitecustomize.py": source_root / "python310_sitecustomize.py",
        "rfdiffusion_runtime_entrypoint.py": root
        / "models/cancer-immunotherapy/runtime-images/rfdiffusion/runtime_entrypoint.py",
    }
    for name, digest in config["source_sha256"].items():
        if (
            hashlib.sha256(
                special.get(name, source_root / name).read_bytes()
            ).hexdigest()
            != digest
        ):
            raise ValueError("captured source bytes changed: " + name)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    profiles = json.loads(
        (
            root / "catalog/runtime/contracts/scientific-workload-profiles.json"
        ).read_bytes()
    )["profiles"]
    profile = next(item for item in profiles if item["model_id"] == "rfdiffusion")
    bundle = {
        key: value
        for key, value in config.items()
        if key not in {"node", "request_mode"}
    }
    bundle.update(
        qualified=True,
        qualification_receipt_sha256=hashlib.sha256(
            args.report.read_bytes()
        ).hexdigest(),
        profile_model_revision=profile["execution_identity"]["model_revision"],
        cli_key="rfdiffusion_observed_cli.py",
        cli_configmap=args.entrypoint_configmap,
        cli_sha256=hashlib.sha256(
            (source_root / "rfdiffusion_observed_cli.py").read_bytes()
        ).hexdigest(),
        entrypoint={
            "configmap": args.entrypoint_configmap,
            "key": "scientific_request_entrypoint.py",
            "sha256": hashlib.sha256(
                (source_root / "scientific_request_entrypoint.py").read_bytes()
            ).hexdigest(),
        },
    )
    from fs2_serve.scientific_batch.startup import validate_bundle

    validate_bundle(bundle, bundle["bundle_id"])
    args.bundle.write_text(json.dumps(bundle, indent=2) + "\n")
    print(json.dumps({"model_id": "rfdiffusion", "statistics": report["statistics"]}))


if __name__ == "__main__":
    main()
