#!/usr/bin/env python3
"""Assemble the two H100 evidence documents from collected run artifacts.

The evidence is generated rather than hand-written so it cannot drift from the
receipts it summarises: every number here comes from ``submit_plan.py`` (live
Kubernetes identities and timings), ``validate_result.py`` (the independent
verdict) or the per-run result envelopes the runtime entrypoint wrote.

Two documents are produced, mirroring the merged Mosaic qualification:

* ``evidence/h100-run-receipt.json`` -- what ran, where, on which device, with
  which artifacts, and how long each phase took.
* ``evidence/h100-semantic-qualification.json`` -- the gate: which variants
  passed, judged by the independent validator, and what remains out of scope.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE.parent / "evidence"
LOCK = HERE.parent / "image-lock.json"


def _load(path: str | None) -> Any:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _envelopes(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {}
    found = {}
    for variant in ("protein", "ligand", "ame"):
        candidate = root / variant / "result.json"
        if candidate.is_file():
            found[variant] = json.loads(candidate.read_text(encoding="utf-8"))
    return found


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_run_receipt(arguments: argparse.Namespace) -> dict[str, Any]:
    lock = _load(str(LOCK))
    runs = _load(arguments.runs)
    envelopes = _envelopes(Path(arguments.outputs) if arguments.outputs else None)
    proving = _load(arguments.contract_proving_runs)

    variants = []
    for run in runs["runs"]:
        name = run["variant"]
        envelope = envelopes.get(name, {})
        verification = envelope.get("artifact_verification") or {}
        variants.append(
            {
                "variant": name,
                "task_name": (envelope.get("target") or {}).get("task_name"),
                "target_structure": (envelope.get("target") or {}).get("target_path"),
                "run_id": envelope.get("run_id"),
                "terminal_state": envelope.get("terminal_state"),
                "upstream_exit_code": envelope.get("upstream_exit_code"),
                "cuda_used_by_upstream": envelope.get("cuda_used_by_upstream"),
                "device": {
                    key: (envelope.get("cuda") or {}).get(key)
                    for key in (
                        "device_name",
                        "compute_capability",
                        "total_memory_bytes",
                        "torch_version",
                        "torch_cuda_version",
                        "architecture_policy",
                    )
                },
                "node": run["node"],
                "container_id": run["container_id"],
                "image": run["image"],
                "image_id": run["image_id"],
                "checkpoint_pair": [
                    {
                        "label": marker["label"],
                        "path": marker["path"],
                        "bytes": marker.get("observed_bytes"),
                        "sha256": marker.get("observed_sha256") or marker.get("expected_sha256"),
                        "content_digest_verified": marker.get("digest_verified"),
                        "digest_seconds": marker.get("digest_seconds"),
                    }
                    for marker in verification.get("markers", [])
                ],
                "rosettafold3": verification.get("rosettafold3"),
                "artifact_verification_seconds": verification.get("seconds"),
                "phases": envelope.get("phases"),
                "kubernetes_timings": run["timings"],
                "image_phase": run["image_phase"],
                "cache_level": envelope.get("cache_level"),
                "asset_link": envelope.get("asset_link"),
                "produced": {
                    "structures": (envelope.get("validation") or {}).get("structure_count"),
                    "chain_lengths": (envelope.get("validation") or {}).get("chain_lengths"),
                    "observed_ligand_residues": (envelope.get("validation") or {}).get(
                        "observed_ligand_residues"
                    ),
                    "reward_rows": (envelope.get("validation") or {}).get("reward_rows"),
                },
                "argv": envelope.get("argv"),
            }
        )

    receipt: dict[str, Any] = {
        "schema": "fs2.nebius.ai/proteina-complexa-h100-run-receipt/v1",
        "owner_task": lock["owner_task"],
        "model_id": lock["model_id"],
        "collected_at": _now(),
        "source": {
            "repository": lock["source"]["repository"],
            "revision": lock["source"]["revision"],
            "archive_sha256": lock["source"]["archive_sha256"],
            "equivalence": lock["source"]["equivalence_check"]["state"],
        },
        "image": {
            "reference": runs["image_reference"],
            "published_digest": lock["image"]["published_digest"],
            "target_tag": lock["image"]["target_tag"],
            "provenance": lock["image"]["provenance"],
            "entrypoint_sha256": runs["entrypoint_sha256"],
        },
        "cluster": {
            "context": "k8s-inference-h100",
            "parent_project": "project-e00rene",
            "region": "eu-north1",
            "namespace": runs["namespace"],
            "kubeconfig": "/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig",
        },
        "artifact_delivery": lock["artifact_delivery"],
        "gpu_snapshot": {
            "captured": False,
            "restored": False,
            "reason": "no device snapshot was captured or restored; the cache levels "
            "reported here are image and artifact locality only",
        },
        "variants": variants,
    }
    if proving:
        receipt["contract_proving_runs"] = {
            "why": "the runtime contract was first proven against the predecessor digest "
            "with the entrypoint overlaid from a ConfigMap, so a rebuild was only "
            "committed to after all three variants had really passed on H100",
            "image_reference": proving["image_reference"],
            "entrypoint_sha256": proving["entrypoint_sha256"],
            "runs": [
                {
                    "variant": run["variant"],
                    "exit_code": run["exit_code"],
                    "schedule_to_semantic_complete_seconds": run["timings"][
                        "schedule_to_semantic_complete_seconds"
                    ],
                    "node": run["node"]["name_sha256"],
                }
                for run in proving["runs"]
            ],
        }
    return receipt


def build_qualification(arguments: argparse.Namespace) -> dict[str, Any]:
    lock = _load(str(LOCK))
    verdict = _load(arguments.verdict)
    proving = _load(arguments.contract_proving_verdict)

    return {
        "schema": "fs2.nebius.ai/proteina-complexa-semantic-qualification/v1",
        "owner_task": lock["owner_task"],
        "model_id": lock["model_id"],
        "collected_at": _now(),
        "image_digest": lock["image"]["published_digest"],
        "source_revision": lock["source"]["revision"],
        "gate": {
            "judged_by": "qualification/validate_result.py, which re-derives every verdict "
            "from the produced artifacts and never imports the runtime entrypoint",
            "requirements": [
                "upstream exited zero and the result envelope reports PASS",
                "the upstream log shows Lightning using CUDA",
                "the exact pinned checkpoint pair appears in the argv and no other "
                "variant's checkpoint does",
                "both checkpoint markers verified, content digests included",
                "LoRA re-applied for ligand and AME, absent for protein",
                "a sampling phase was measured",
                "at least one produced chain has protein-like C-alpha geometry",
                "chains of 20+ residues carry at least five distinct amino-acid types",
                "the designed binder falls inside the target's declared length envelope",
                "the expected ligand residue is present for ligand and AME",
            ],
        },
        "all_variants_passed": verdict["all_passed"],
        "variants": [
            {
                "variant": item["variant"],
                "passed": item["passed"],
                "checkpoint_pair": item["checkpoint_pair"],
                "content_digests_verified": item["content_digests_verified"],
                "cuda_marker_in_log": item["cuda_marker_in_log"],
                "phases": item["phases"],
                "chain_lengths": item["chain_lengths"],
                "chain_distinct_residues": item.get("chain_distinct_residues"),
                "binder_length_envelope": item["binder_length_envelope"],
                "expected_ligands": item.get("expected_ligands"),
                "observed_non_standard_residues": item.get("observed_non_standard_residues"),
                "rosettafold3_bound": item["rosettafold3_bound"],
                "rosettafold3_exercised": item["rosettafold3_exercised"],
                "failures": item["failures"],
            }
            for item in verdict["variants"]
        ],
        "scope": {
            "covered": "one bounded single-sample generation per variant at the upstream "
            "default 400 sampling steps, reward-free so the Complexa score model is "
            "isolated from the AlphaFold2 and RosettaFold3 reward models",
            "not_covered": [
                "throughput, batch scaling and multi-GPU behaviour",
                "the AlphaFold2 reward path for the protein pipeline and the "
                "RosettaFold3 reward path for ligand and AME; RF3 is bound and "
                "marker-verified on every run but not exercised",
                "the filter, evaluate and analyze pipeline stages, which need "
                "foldseek, mmseqs, sc and dssp -- none of which are in this image",
                "fast-start tiers, GPU snapshot and restore, preemption and contention",
                "the scientific batch controller route",
            ],
        },
        "servable": False,
        "servable_gate": lock["image"]["qualification"]["servable_gate"],
        "contract_proving_verdict": (
            {
                "image_digest": "sha256:f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca",
                "all_variants_passed": proving["all_passed"],
                "note": "the same gate run against the predecessor digest with the "
                "entrypoint overlaid from a ConfigMap; this is what established that "
                "the recipe works before a rebuild was committed to. The predecessor "
                "remains not deployable: it bakes no batch contract and carries no "
                "attached SLSA provenance.",
            }
            if proving
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, help="submit_plan.py output for the accepted run")
    parser.add_argument("--verdict", required=True, help="validate_result.py output")
    parser.add_argument("--outputs", default=None, help="directory holding per-variant outputs")
    parser.add_argument("--contract-proving-runs", default=None)
    parser.add_argument("--contract-proving-verdict", default=None)
    arguments = parser.parse_args()

    EVIDENCE.mkdir(exist_ok=True)
    receipt = build_run_receipt(arguments)
    qualification = build_qualification(arguments)
    (EVIDENCE / "h100-run-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    (EVIDENCE / "h100-semantic-qualification.json").write_text(
        json.dumps(qualification, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "run_receipt_variants": len(receipt["variants"]),
                "all_variants_passed": qualification["all_variants_passed"],
                "image_digest": qualification["image_digest"],
            },
            indent=2,
        )
    )
    return 0 if qualification["all_variants_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
