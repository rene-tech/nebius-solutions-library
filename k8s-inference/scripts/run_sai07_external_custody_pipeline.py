#!/usr/bin/env python3
"""Run the separately administered SAI-07 custody adoption or ledger CAS.

This entrypoint is intentionally unusable while the repository trust lock is
inactive.  It accepts no authority-key, owner, IAM, provider or backend identity
overrides.  Adoption rejects delete/replace actions and retains its generation-
named saved plan. Authorization first validates the exact v2 signed handoff and
then delegates the one-time live-read/ConfigMap-resourceVersion CAS to
``verify_pod_security_receipts.py`` under the external receipt identity.

This task only adds the source contract; it did not execute this program.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CUSTODY_ROOT = ROOT / "stages" / "pod-security-custody"
TRUST_LOCK = CUSTODY_ROOT / "custody-trust-lock.json"


class PipelineError(ValueError):
    pass


def run(command: list[str], *, stdin: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
    completed = subprocess.run(
        command,
        input=stdin,
        check=False,
        capture_output=True,
        timeout=900,
        env=env,
    )
    if completed.returncode != 0:
        raise PipelineError(f"custody pipeline command failed: {command[0]} {command[1] if len(command) > 1 else ''}")
    return completed.stdout


def canonical_file(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    from verify_sai07_custody_manifest_bundle import canonical, read_regular

    payload = read_regular(path, label)
    value = json.loads(payload)
    if not isinstance(value, dict) or canonical(value) != payload:
        raise PipelineError(f"{label} must be canonical JSON")
    return payload, value


def verify_trust(args: argparse.Namespace) -> dict[str, str]:
    query = {
        "trust_lock_path": str(TRUST_LOCK),
        "iam_boundary_receipt_path": str(args.iam_boundary_receipt),
        "backend_custody_receipt_path": str(args.backend_custody_receipt),
        "owner_kubeconfig_path": str(args.owner_kubeconfig),
        "owner_context": args.owner_context,
    }
    output = run(
        [sys.executable, str(ROOT / "scripts" / "verify_sai07_custody_trust.py")],
        stdin=json.dumps(query, sort_keys=True, separators=(",", ":")).encode(),
    )
    result = json.loads(output)
    if not isinstance(result, dict) or result.get("valid") != "true":
        raise PipelineError("external custody trust verification did not succeed")
    return result


def adoption(args: argparse.Namespace) -> None:
    trust = verify_trust(args)
    query = {
        "bundle_path": str(args.manifest_bundle),
        "verified_trust_json": json.dumps(trust, sort_keys=True, separators=(",", ":")),
        "owner_kubeconfig_path": str(args.owner_kubeconfig),
        "owner_context": args.owner_context,
    }
    verified = json.loads(
        run(
            [sys.executable, str(ROOT / "scripts" / "verify_sai07_custody_manifest_bundle_v2.py")],
            stdin=json.dumps(query, sort_keys=True, separators=(",", ":")).encode(),
        )
    )
    if verified.get("valid") != "true" or verified.get("platform_state_object_count") != verified.get("refreshed_live_object_count"):
        raise PipelineError("exhaustive immediate pre-SSA verification failed")
    if args.saved_plan.exists():
        raise PipelineError("generation-named saved plan already exists; overwrite is forbidden")
    common = ["terraform", f"-chdir={CUSTODY_ROOT}"]
    run(
        [
            *common,
            "init",
            "-input=false",
            f"-backend-config={args.backend_config}",
        ]
    )
    run(
        [
            *common,
            "plan",
            "-input=false",
            "-lock=true",
            f"-out={args.saved_plan}",
            f"-var=owner_kubeconfig_path={args.owner_kubeconfig}",
            f"-var=owner_context={args.owner_context}",
            f"-var=manifest_bundle_path={args.manifest_bundle}",
            f"-var=iam_boundary_receipt_path={args.iam_boundary_receipt}",
            f"-var=backend_custody_receipt_path={args.backend_custody_receipt}",
        ]
    )
    plan = json.loads(run([*common, "show", "-json", str(args.saved_plan)]))
    changes = plan.get("resource_changes")
    if not isinstance(changes, list) or not changes:
        raise PipelineError("saved plan contains no reviewable resource changes")
    forbidden = []
    for change in changes:
        actions = change.get("change", {}).get("actions", []) if isinstance(change, dict) else []
        if "delete" in actions or actions not in (["no-op"], ["create"], ["update"], ["read"]):
            forbidden.append(change.get("address", "<malformed>"))
    if forbidden:
        raise PipelineError("saved plan contains delete, replacement, or unsupported actions")
    run([*common, "apply", "-input=false", "-lock=true", str(args.saved_plan)])
    # Deliberately stop after SSA. A separately authenticated collector must
    # immediately reread the exact set, run all five v2 authority audits, and
    # obtain the external signature. The resulting handoff enters `authorize`.
    sys.stdout.write(json.dumps({"valid": "true", "stage": "adopted-awaiting-signed-post-read", "pre_objects_sha256": verified["platform_state_objects_sha256"]}, sort_keys=True) + "\n")


def authorize(args: argparse.Namespace) -> None:
    trust = verify_trust(args)
    _, request = canonical_file(args.authorization_request, "authorization request")
    handoff_query = request.get("handoff_query")
    receipt_query = request.get("receipt_query")
    if not isinstance(handoff_query, dict) or not isinstance(receipt_query, dict):
        raise PipelineError("authorization request omits handoff or receipt query")
    if handoff_query.get("trust_lock_path") != str(TRUST_LOCK):
        raise PipelineError("authorization request does not use the repository trust lock")
    if request.get("receipt_operator_username") != trust["receipt_username"]:
        raise PipelineError("authorization request receipt identity differs from external IAM custody")
    handoff = json.loads(
        run(
            [sys.executable, str(ROOT / "scripts" / "verify_sai07_external_handoff_v2.py")],
            stdin=json.dumps(handoff_query, sort_keys=True, separators=(",", ":")).encode(),
        )
    )
    if handoff.get("valid") != "true":
        raise PipelineError("exact post-adoption handoff did not verify")
    env = dict(os.environ)
    env.update(
        {
            "FS2_KUBECONFIG": str(args.receipt_operator_kubeconfig),
            "FS2_KUBE_CONTEXT": args.owner_context,
            "FS2_POD_SECURITY_CUSTODY_USER": request["receipt_operator_username"],
            "FS2_POD_SECURITY_TOKEN_AUDIENCE": "https://kubernetes.default.svc",
            "FS2_POD_SECURITY_TOKEN_ANCHOR_UID": request["token_anchor_uid"],
            "FS2_POD_SECURITY_QUERY": json.dumps(receipt_query, sort_keys=True, separators=(",", ":")),
        }
    )
    cas = json.loads(run([sys.executable, str(ROOT / "scripts" / "verify_pod_security_receipts.py")], env=env))
    if cas.get("valid") != "true" or cas.get("bundle_sha256") != handoff.get("bundle_sha256"):
        raise PipelineError("ledger CAS result differs from the exact signed handoff")
    sys.stdout.write(json.dumps(cas, sort_keys=True, separators=(",", ":")) + "\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("mode", choices=("adopt", "authorize"))
    result.add_argument("--iam-boundary-receipt", required=True, type=Path)
    result.add_argument("--backend-custody-receipt", required=True, type=Path)
    result.add_argument("--owner-kubeconfig", required=True, type=Path)
    result.add_argument("--owner-context", required=True)
    result.add_argument("--manifest-bundle", type=Path)
    result.add_argument("--secret-metadata-artifact", type=Path)
    result.add_argument("--backend-config", type=Path)
    result.add_argument("--saved-plan", type=Path)
    result.add_argument("--authorization-request", type=Path)
    result.add_argument("--receipt-operator-kubeconfig", type=Path)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        if args.mode == "adopt":
            required = (args.manifest_bundle, args.backend_config, args.saved_plan)
            if any(value is None or not value.is_absolute() for value in required):
                raise PipelineError("adopt mode requires absolute manifest, backend and saved-plan paths")
            adoption(args)
        else:
            if args.authorization_request is None or args.receipt_operator_kubeconfig is None:
                raise PipelineError("authorize mode requires an authorization request and receipt-operator kubeconfig")
            authorize(args)
    except (PipelineError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"SAI-07 external custody pipeline rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
