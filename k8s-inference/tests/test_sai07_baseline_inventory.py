from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "inventory", ROOT / "scripts" / "audit_sai07_baseline_inventory.py"
)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def test_exact_frozen_scientific_namespace_inventory() -> None:
    assert inventory.SCIENTIFIC_NAMESPACES == (
        "fs2-bioir-boltz2",
        "fs2-bioir-coverage",
        "fs2-bioir-openfold",
        "fs2-bioir-protenix",
        "fs2-bioir-snapshot",
    )


def test_baseline_scanner_rejects_every_retained_unsafe_shape() -> None:
    spec = {
        "hostNetwork": True,
        "hostPID": True,
        "hostIPC": True,
        "volumes": [{"hostPath": {"path": "/mnt/fs2-reference-data/data"}}],
        "initContainers": [
            {"securityContext": {"privileged": True, "capabilities": {"add": ["SYS_ADMIN"]}}}
        ],
        "containers": [
            {
                "ports": [{"hostPort": 8080}],
                "securityContext": {
                    "procMount": "Unmasked",
                    "seccompProfile": {"type": "Unconfined"},
                },
            }
        ],
    }
    assert inventory.baseline_findings(spec) == [
        "capabilities",
        "hostIPC",
        "hostNetwork",
        "hostPID",
        "hostPath",
        "hostPort",
        "privileged",
        "procMount",
        "unconfinedSeccomp",
    ]


def test_reviewed_baseline_pod_has_no_findings() -> None:
    spec = {
        "automountServiceAccountToken": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "volumes": [{"persistentVolumeClaim": {"claimName": "model-cache"}}],
        "containers": [
            {
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                }
            }
        ],
    }
    assert inventory.baseline_findings(spec) == []
