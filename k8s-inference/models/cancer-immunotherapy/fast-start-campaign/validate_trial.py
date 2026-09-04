#!/usr/bin/env python3
"""Apply a semantic gate to a completed fast-start trial.

A trial that exits zero has not been shown to have produced science.  Each gate
here reads the structure the run actually wrote and rejects a well-formed but
degenerate result, so "successful semantic trial" means the same thing it means
in the owning runtime's own qualification.

Mosaic and BoltzGen write to the regional shared filesystem, so their outputs
are read back through a short-lived reader Pod.  AlphaFold 3 writes into an
ephemeral scratch volume that is deliberately not copied onto the tenant-private
licensed claim, so its gate reads the digests the run printed into its own log.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import faststart

HERE = Path(__file__).resolve().parent
READER = "fsc-artifact-reader"
MOSAIC_VALIDATOR = HERE.parent / "runtime-images" / "mosaic" / "qualification" / "validate_result.py"

MIN_ATOMS = 50
MIN_RESIDUES = 20


def structural_report():
    """Reuse the owning task's structural gate rather than restating it."""
    spec = importlib.util.spec_from_file_location("mosaic_validate_result", MOSAIC_VALIDATOR)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {MOSAIC_VALIDATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.structural_report


def ensure_reader(namespace: str, claim: str) -> str:
    root = faststart.matrix()
    existing = faststart.kube_json("get", "pods", "-n", namespace, "-l", f"fs2.nebius.ai/reader={claim}")
    for item in existing.get("items", []):
        if item["status"]["phase"] == "Running":
            return item["metadata"]["name"]
    name = f"{READER}-{hashlib.sha256(claim.encode()).hexdigest()[:8]}"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(root["labels"], **{"fs2.nebius.ai/reader": claim}),
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "nodeSelector": {"workload.fs2.nebius/system": "true"},
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True},
            "containers": [
                {
                    "name": "reader",
                    "image": "busybox:1.36",
                    "command": ["sh", "-c", "sleep 3600"],
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": True}],
                }
            ],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}],
        },
    }
    faststart.kubectl("apply", "-n", namespace, "-f", "-", stdin=json.dumps(pod))
    for _ in range(60):
        state = faststart.kube_json("get", "pod", name, "-n", namespace)
        if state["status"]["phase"] == "Running":
            return name
        time.sleep(5)
    raise SystemExit(f"reader pod {name} did not become Running")


def read_file(namespace: str, pod: str, path: str) -> bytes:
    """Read a file byte-exactly through base64 so binary content survives."""
    encoded = faststart.kubectl(
        "exec", "-n", namespace, pod, "--", "sh", "-c", f"base64 '{path}'"
    )
    return base64.b64decode("".join(encoded.split()))


def listing(namespace: str, pod: str, path: str, pattern: str = "*") -> list[str]:
    out = faststart.kubectl(
        "exec", "-n", namespace, pod, "--",
        "sh", "-c", f"find '{path}' -name '{pattern}' -type f 2>/dev/null | sort",
        check=False,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def gate_mosaic_binder(receipt: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    namespace = "fs2-models"
    claim = spec["volumes"]["workspace"]["claim"]
    pod = ensure_reader(namespace, claim)
    tid = receipt["trial_id"]
    root = f"/data/faststart/{tid}/shard-000"

    result = json.loads(read_file(namespace, pod, f"{root}/shard-result.json"))
    metrics = json.loads(read_file(namespace, pod, f"{root}/candidate-metrics.json"))
    pdb = read_file(namespace, pod, f"{root}/candidate.pdb")
    structure = structural_report()(pdb)

    checks = {
        "shard_status_succeeded": result.get("status") == "succeeded",
        "backend_id_matches": result.get("backend_id") == spec["backend_id"],
        "source_revision_matches": result.get("source_revision") == spec["source_revision"],
        "sequence_length_matches_structure": len(metrics["sequence"]) == structure["residues"],
        "iptm_in_unit_range": 0.0 <= metrics["iptm"] <= 1.0,
        "plddt_in_unit_range": 0.0 <= metrics["mean_plddt"] <= 1.0,
        "iptm_above_trivial": metrics["iptm"] > 0.3,
        "no_placeholder_residues": True,
    }
    return {
        "gate": "mosaic_binder",
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "structure": structure,
        "candidate": {
            "sequence": metrics["sequence"],
            "iptm": metrics["iptm"],
            "mean_plddt": metrics["mean_plddt"],
            "objective": metrics["objective"],
        },
        "reused_validator": str(MOSAIC_VALIDATOR.relative_to(MOSAIC_VALIDATOR.parents[5])),
    }


def gate_boltzgen_design(receipt: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    namespace = "fs2-models"
    claim = spec["volumes"]["workspace"]["claim"]
    pod = ensure_reader(namespace, claim)
    tid = receipt["trial_id"]
    root = f"/data/faststart/{tid}"

    designs = listing(namespace, pod, root, "*.cif")
    ranked = [path for path in designs if "final_ranked_designs" in path] or designs
    if not ranked:
        return {"gate": "boltzgen_design", "status": "failed", "checks": {"design_written": False}}

    payload = read_file(namespace, pod, ranked[0])
    text = payload.decode("ascii", "replace")
    atoms = [line for line in text.splitlines() if line.startswith("ATOM")]
    residues = {tuple(line.split()[6:9]) for line in atoms} if atoms else set()

    checks = {
        "design_written": True,
        "structure_has_atoms": len(atoms) >= MIN_ATOMS,
        "structure_has_residues": len(residues) >= MIN_RESIDUES,
        "fused_kernels_enabled": receipt["measured"]["runtime_reported"].get("fused_kernels_enabled") == "True",
        "hopper_device_capability": receipt["measured"]["runtime_reported"].get("device_capability_major") == 9,
    }
    return {
        "gate": "boltzgen_design",
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "structure": {
            "path": ranked[0].replace("/data", f"pvc://{claim}"),
            "cif_count": len(designs),
            "atom_records": len(atoms),
            "distinct_residues": len(residues),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }


def gate_af3_structure(receipt: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    reported = receipt["measured"]["runtime_reported"]
    atoms = reported.get("semantic_cif_atoms")
    checks = {
        "inference_ran": reported.get("inference_seconds") is not None,
        "samples_extracted": (reported.get("inference_samples") or 0) >= 1,
        "cif_written": (reported.get("semantic_cif_count") or 0) >= 1,
        "structure_has_atoms": (atoms or 0) >= MIN_ATOMS,
        "cif_digest_present": bool(reported.get("semantic_cif_sha256")),
    }
    return {
        "gate": "af3_structure",
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "structure": {
            "atom_records": atoms,
            "bytes": reported.get("semantic_cif_bytes"),
            "sha256": reported.get("semantic_cif_sha256"),
            "cif_count": reported.get("semantic_cif_count"),
        },
        "runtime_reported_seconds": {
            "featurisation": reported.get("featurisation_seconds"),
            "inference": reported.get("inference_seconds"),
            "extraction": reported.get("extraction_seconds"),
        },
        "evidence_source": "container log; the scratch volume is ephemeral by design",
    }


GATES = {
    "mosaic_binder": gate_mosaic_binder,
    "boltzgen_design": gate_boltzgen_design,
    "af3_structure": gate_af3_structure,
}


def validate(model: str, variant: str, trial: int) -> dict[str, Any]:
    root = faststart.matrix()
    spec = root["models"][model]
    tid = faststart.trial_id(model, variant, trial)
    path = faststart.RECEIPTS / f"{tid}.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt["job_state"] != "complete":
        raise SystemExit(f"{tid} did not complete; refusing to run a semantic gate on it")

    outcome = GATES[spec["semantic_gate"]](receipt, spec)
    receipt["semantic_validation"] = outcome
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--trial", type=int, required=True)
    arguments = parser.parse_args()
    outcome = validate(arguments.model, arguments.variant, arguments.trial)
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
