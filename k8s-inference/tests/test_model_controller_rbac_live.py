"""Opt-in authorization checks against an integrated Kubernetes release.

The test is intentionally skipped in ordinary offline CI. A rollout reviewer
supplies the exact kubeconfig only after the SAI-03 runtime-profile lineage and
this RBAC lineage have been integrated.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


KUBECONFIG = os.environ.get("FS2_RBAC_INTEGRATION_KUBECONFIG")
SERVICE_ACCOUNT = os.environ.get(
    "FS2_MODEL_CONTROLLER_SERVICE_ACCOUNT",
    "system:serviceaccount:fs2-system:fs2-serve-control-plane-controller",
)

pytestmark = pytest.mark.skipif(
    KUBECONFIG is None,
    reason="set FS2_RBAC_INTEGRATION_KUBECONFIG for a read-only authorization integration run",
)


def _can_i(verb: str, resource: str, *, api_group: str | None = None) -> bool:
    assert KUBECONFIG is not None
    command = [
        "kubectl",
        "--kubeconfig",
        str(Path(KUBECONFIG)),
        "auth",
        "can-i",
        verb,
        resource,
        "--namespace",
        "fs2-models",
        "--as",
        SERVICE_ACCOUNT,
    ]
    if api_group is not None:
        command.extend(["--api-group", api_group])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)  # noqa: S603
    answer = completed.stdout.strip().lower()
    assert answer in {"yes", "no"}, completed.stderr
    return answer == "yes"


def test_model_controller_service_account_has_zero_network_policy_authority() -> None:
    for verb in ("get", "list", "watch", "create", "patch", "delete"):
        assert not _can_i(verb, "networkpolicies", api_group="networking.k8s.io"), verb


def test_model_controller_retains_only_required_runtime_writer_authority() -> None:
    for verb in ("get", "list", "watch", "create", "patch", "delete"):
        assert _can_i(verb, "deployments", api_group="apps"), verb
        assert _can_i(verb, "services"), verb
        assert _can_i(verb, "scaledobjects", api_group="keda.sh"), verb
