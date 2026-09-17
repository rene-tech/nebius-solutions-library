#!/usr/bin/env python3
"""Read-only verification entrypoint for the fixed operator viewer handoff.

The production authority selects the provider identity, kubeconfig, project,
cluster and executables. This client has no issue, create, reconcile, revoke,
delete, profile, project, path or key-id option and writes no local artifact.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


AUTHORITY_CLIENT = Path("/opt/fs2/k8s-inference/scripts/credential_provider_adapter.py")


class HandoffVerificationError(RuntimeError):
    pass


def verify() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["/usr/bin/python3", str(AUTHORITY_CLIENT)],
            input='{"operation":"operator-proxy-context"}',
            text=True,
            capture_output=True,
            check=True,
            timeout=120,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
            },
        )
        observation = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise HandoffVerificationError(
            "operator viewer authority observation failed"
        ) from error
    identity = observation.get("operator_identity")
    denials = observation.get("denials")
    inventory = observation.get("inventory")
    if (
        not isinstance(identity, dict)
        or identity.get("role") != "viewer"
        or not isinstance(identity.get("key_id"), str)
        or not identity["key_id"]
        or not isinstance(identity.get("expires_at"), str)
        or not isinstance(denials, dict)
        or not denials
        or not all(value is True for value in denials.values())
        or not isinstance(inventory, dict)
        or inventory.get("allowed") is not True
        or not isinstance(observation.get("allowed_cidrs"), list)
        or not observation["allowed_cidrs"]
    ):
        raise HandoffVerificationError(
            "operator viewer proof is incomplete or retains forbidden access"
        )
    return {
        "status": "verified-read-only",
        "project_id": identity["project_id"],
        "cluster_id": identity["cluster_id"],
        "service_account_id": identity["service_account_id"],
        "key_id": identity["key_id"],
        "expires_at": identity["expires_at"],
        "provider_bindings_sha256": identity["provider_bindings_sha256"],
        "denial_matrix_sha256": __import__("hashlib").sha256(
            json.dumps(denials, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "inventory_sha256": inventory["resource_names_sha256"],
        "allowed_cidrs": observation["allowed_cidrs"],
    }


def main() -> int:
    if sys.argv != [sys.argv[0], "verify"]:
        raise HandoffVerificationError("the only supported command is verify")
    print(json.dumps(verify(), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HandoffVerificationError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error

