#!/usr/bin/env python3
"""Prepare a non-state-forgetting SAI-07 external execution.

This v3 entrypoint is deliberately verification-only.  It verifies raw
provider/backend evidence and the immediately refreshed manifest bundle, then
emits a canonical preflight for the separately administered v3 executor.
It never invokes Terraform, imports an object, removes a state address, writes
the rollout ledger, or mutates Kubernetes.  The executor owns zero fields on
Terraform-retained objects: it may create only one immutable,
generation-addressed acknowledgement and must prove every retained object is
byte-for-byte unchanged across that SSA.  Activation remains blocked until the
executor and provider-native custody facts are reviewed and pinned.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TRUST_LOCK = ROOT / "stages" / "pod-security-custody" / "custody-trust-lock-v3.json"
STATE_HANDOFF = ROOT / "stages" / "foundation" / "pod_security_custody_state_handoff.tf"


class PipelineV3Error(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def run_verifier(script: str, query: dict[str, str]) -> dict[str, str]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script)],
        input=canonical(query),
        check=False,
        capture_output=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise PipelineV3Error(f"{script} rejected the handoff")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PipelineV3Error(f"{script} returned invalid JSON") from error
    if not isinstance(value, dict) or value.get("valid") != "true":
        raise PipelineV3Error(f"{script} did not return an accepted result")
    return value


def active_source_without_archive(path: Path) -> str:
    source = path.read_text()
    if source.count("/*") != 1 or source.count("*/") != 1:
        raise PipelineV3Error("state-handoff negative-evidence archive is malformed")
    before, remainder = source.split("/*", 1)
    _, after = remainder.rsplit("*/", 1)
    return before + after


def trust_query(args: argparse.Namespace, prefix: str) -> dict[str, str]:
    return {
        "backend_evidence_path": str(getattr(args, f"{prefix}_backend_evidence")),
        "backend_receipt_path": str(getattr(args, f"{prefix}_backend_receipt")),
        "platform_state_path": str(getattr(args, f"{prefix}_platform_state")),
        "provider_evidence_path": str(getattr(args, f"{prefix}_provider_evidence")),
        "provider_receipt_path": str(getattr(args, f"{prefix}_provider_receipt")),
        "trust_lock_path": str(TRUST_LOCK),
    }


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    prior = run_verifier("verify_sai07_custody_trust_v3.py", trust_query(args, "prior"))
    trust = run_verifier("verify_sai07_custody_trust_v3.py", trust_query(args, "current"))
    stable = {
        "authority_key_id",
        "authority_public_key_sha256",
        "backend_projection_sha256",
        "cluster_id",
        "contract_sha256",
        "custody_epoch_generation",
        "custody_epoch_id",
        "custody_epoch_principal_id",
        "custody_epoch_sha256",
        "custody_addresses_json",
        "custody_state_objects_json",
        "kube_system_uid",
        "namespace_inventory_json",
        "owner_groups_json",
        "owner_username",
        "platform_groups_json",
        "platform_username",
        "persistent_volume_names_json",
        "provider_projection_sha256",
        "receipt_groups_json",
        "receipt_username",
        "state_addresses_sha256",
        "state_all_addresses_sha256",
        "state_all_object_count",
        "state_etag",
        "state_lineage",
        "state_object_count",
        "state_object_version",
        "state_objects_sha256",
        "state_serial",
        "state_sha256",
    }
    drift = sorted(field for field in stable if prior.get(field) != trust.get(field))
    if drift:
        raise PipelineV3Error(f"provider/backend/state drifted across the two signed collections: {','.join(drift)}")
    if prior["collection_id"] == trust["collection_id"]:
        raise PipelineV3Error("drift fence requires two independently collected generation IDs")
    if prior["backend_completed_at"] >= trust["provider_completed_at"]:
        raise PipelineV3Error("current evidence does not follow the completed prior collection")
    manifest_query = {
        "bundle_path": str(args.manifest_bundle),
        "verified_trust_json": json.dumps(trust, sort_keys=True, separators=(",", ":")),
    }
    manifests = run_verifier("verify_sai07_custody_manifest_bundle_v3.py", manifest_query)
    if manifests.get("platform_state_retained") != "true":
        raise PipelineV3Error("manifest verifier did not retain platform state ownership")
    active_handoff_source = active_source_without_archive(STATE_HANDOFF)
    forbidden = ("removed {", "terraform state rm", "terraform import", "import {")
    if any(item in active_handoff_source for item in forbidden):
        raise PipelineV3Error("active platform source contains a state-forgetting/adoption primitive")
    return {
        "action": "await-external-acknowledgement-ssa",
        "collection_id": trust["collection_id"],
        "contract_sha256": trust["contract_sha256"],
        "custody_epoch_generation": trust["custody_epoch_generation"],
        "custody_epoch_id": trust["custody_epoch_id"],
        "custody_epoch_principal_id": trust["custody_epoch_principal_id"],
        "custody_epoch_sha256": trust["custody_epoch_sha256"],
        "custody_field_ownership": "zero-fields-on-platform-state",
        "manifest_bundle_sha256": manifests["bundle_sha256"],
        "manifest_objects_sha256": manifests["objects_sha256"],
        "platform_state_addresses_sha256": trust["state_addresses_sha256"],
        "platform_state_objects_sha256": trust["state_objects_sha256"],
        "platform_state_all_addresses_sha256": trust["state_all_addresses_sha256"],
        "platform_state_all_object_count": trust["state_all_object_count"],
        "platform_state_lineage": trust["state_lineage"],
        "platform_state_serial": trust["state_serial"],
        "platform_state_version": trust["state_object_version"],
        "state_ownership": "platform-retained-no-import-no-forget",
        "prior_collection_id": prior["collection_id"],
        "provider_backend_drift_fenced": "true",
        "valid": "true",
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for prefix in ("prior", "current"):
        result.add_argument(f"--{prefix}-provider-evidence", required=True, type=Path)
        result.add_argument(f"--{prefix}-backend-evidence", required=True, type=Path)
        result.add_argument(f"--{prefix}-platform-state", required=True, type=Path)
        result.add_argument(f"--{prefix}-provider-receipt", required=True, type=Path)
        result.add_argument(f"--{prefix}-backend-receipt", required=True, type=Path)
    result.add_argument("--manifest-bundle", required=True, type=Path)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        evidence_paths = tuple(
            getattr(args, f"{prefix}_{field}")
            for prefix in ("prior", "current")
            for field in (
                "provider_evidence",
                "backend_evidence",
                "platform_state",
                "provider_receipt",
                "backend_receipt",
            )
        )
        for path in (*evidence_paths, args.manifest_bundle):
            if not path.is_absolute() or ".." in path.parts:
                raise PipelineV3Error("all evidence/credential paths must be absolute without traversal")
        result = prepare(args)
    except (PipelineV3Error, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"SAI-07 retained-state custody v3 rejected: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
