#!/usr/bin/env python3
"""Produce a canonical, read-only pre-enforcement SAI-07 inventory.

The result is deliberately evidence, not rollout authority.  Its canonical
SHA-256 and zero-valued counters are embedded in the independently signed
``baseline-ready`` transition.  The collector refuses a changed fs2-bioir
namespace set and inspects both standalone Pods and controller Pod templates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = "fs2-serve.nebius.ai/sai07-baseline-inventory/v1"
SCIENTIFIC_NAMESPACES = (
    "fs2-bioir-boltz2",
    "fs2-bioir-coverage",
    "fs2-bioir-openfold",
    "fs2-bioir-protenix",
    "fs2-bioir-snapshot",
)
BASELINE_NAMESPACES = (
    "fs2-data",
    "fs2-models",
    "fs2-observability",
    "fs2-reference-data",
    "fs2-system",
    *SCIENTIFIC_NAMESPACES,
)
EXCEPTION_NAMESPACE = "fs2-node-observability"
EXCEPTION_OWNERS = {
    "fs2-dcgm-exporter": "fs2-dcgm-exporter",
    "fs2-node-exporter": "fs2-node-exporter",
    "fs2-otel-node-agent": "fs2-otel-node",
    "fs2-serve-control-plane-gpu-observer": "fs2-serve-control-plane-gpu-observer",
}
COLLECTIONS = {
    "Pod": ("/api/v1/namespaces/{namespace}/pods", ("spec",)),
    "Deployment": ("/apis/apps/v1/namespaces/{namespace}/deployments", ("spec", "template", "spec")),
    "StatefulSet": ("/apis/apps/v1/namespaces/{namespace}/statefulsets", ("spec", "template", "spec")),
    "DaemonSet": ("/apis/apps/v1/namespaces/{namespace}/daemonsets", ("spec", "template", "spec")),
    "Job": ("/apis/batch/v1/namespaces/{namespace}/jobs", ("spec", "template", "spec")),
    "CronJob": ("/apis/batch/v1/namespaces/{namespace}/cronjobs", ("spec", "jobTemplate", "spec", "template", "spec")),
}
BASELINE_CAPABILITIES = {
    "AUDIT_WRITE",
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "MKNOD",
    "NET_BIND_SERVICE",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
    "SYS_CHROOT",
}


class InventoryError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class Kubectl:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        self.base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]

    def raw(self, uri: str) -> dict[str, Any]:
        result = subprocess.run(
            [*self.base, "get", "--raw", uri], check=False, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise InventoryError(f"read failed for {uri}: {result.stderr.strip()}")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise InventoryError(f"read returned a non-object for {uri}")
        return value


def nested(value: object, path: tuple[str, ...]) -> dict[str, Any]:
    current = value
    for component in path:
        current = current.get(component, {}) if isinstance(current, dict) else {}
    return current if isinstance(current, dict) else {}


def baseline_findings(spec: dict[str, Any]) -> list[str]:
    findings: set[str] = set()
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(field) is True:
            findings.add(field)
    for volume in spec.get("volumes", []) or []:
        if isinstance(volume, dict) and "hostPath" in volume:
            findings.add("hostPath")
    containers = [
        *(spec.get("initContainers", []) or []),
        *(spec.get("containers", []) or []),
        *(spec.get("ephemeralContainers", []) or []),
    ]
    for container in containers:
        security = container.get("securityContext", {}) if isinstance(container, dict) else {}
        if security.get("privileged") is True:
            findings.add("privileged")
        if security.get("procMount") not in (None, "Default"):
            findings.add("procMount")
        seccomp = security.get("seccompProfile", {})
        if isinstance(seccomp, dict) and seccomp.get("type") == "Unconfined":
            findings.add("unconfinedSeccomp")
        apparmor = security.get("appArmorProfile", {})
        if isinstance(apparmor, dict) and apparmor.get("type") == "Unconfined":
            findings.add("unconfinedAppArmor")
        added = set((security.get("capabilities", {}) or {}).get("add", []) or [])
        if not added.issubset(BASELINE_CAPABILITIES):
            findings.add("capabilities")
        for port in container.get("ports", []) if isinstance(container, dict) else []:
            if isinstance(port, dict) and int(port.get("hostPort", 0) or 0) != 0:
                findings.add("hostPort")
    pod_security = spec.get("securityContext", {}) or {}
    seccomp = pod_security.get("seccompProfile", {}) if isinstance(pod_security, dict) else {}
    if isinstance(seccomp, dict) and seccomp.get("type") == "Unconfined":
        findings.add("unconfinedSeccomp")
    return sorted(findings)


def inventory(client: Kubectl) -> dict[str, Any]:
    namespaces = client.raw("/api/v1/namespaces")
    discovered = sorted(
        item.get("metadata", {}).get("name", "")
        for item in namespaces.get("items", [])
        if item.get("metadata", {}).get("name", "").startswith("fs2-bioir-")
    )
    if discovered != list(SCIENTIFIC_NAMESPACES):
        raise InventoryError(
            "live fs2-bioir namespace inventory differs from the exact frozen five-name contract"
        )

    objects: list[dict[str, Any]] = []
    reference_host_paths = 0
    incompatible_objects = 0
    for namespace in BASELINE_NAMESPACES:
        for kind, (uri, path) in COLLECTIONS.items():
            collection = client.raw(uri.format(namespace=namespace))
            for item in collection.get("items", []):
                spec = nested(item, path)
                findings = baseline_findings(spec)
                if findings:
                    incompatible_objects += 1
                if "hostPath" in findings:
                    reference_host_paths += 1
                metadata = item.get("metadata", {})
                objects.append(
                    {
                        "kind": kind,
                        "namespace": namespace,
                        "name": metadata.get("name"),
                        "uid": metadata.get("uid"),
                        "pod_spec_sha256": hashlib.sha256(canonical(spec)).hexdigest(),
                        "baseline_findings": findings,
                    }
                )

    unauthorized_exception: list[str] = []
    for kind, (uri, path) in COLLECTIONS.items():
        collection = client.raw(uri.format(namespace=EXCEPTION_NAMESPACE))
        for item in collection.get("items", []):
            metadata = item.get("metadata", {})
            name = str(metadata.get("name", ""))
            spec = nested(item, path)
            if kind == "DaemonSet":
                if name not in EXCEPTION_OWNERS or spec.get("serviceAccountName") != EXCEPTION_OWNERS.get(name):
                    unauthorized_exception.append(f"{kind}/{name}")
            elif kind == "Pod":
                owners = metadata.get("ownerReferences", []) or []
                owner_names = {
                    owner.get("name")
                    for owner in owners
                    if isinstance(owner, dict) and owner.get("kind") == "DaemonSet"
                }
                expected = next((owner for owner in owner_names if owner in EXCEPTION_OWNERS), None)
                if expected is None or spec.get("serviceAccountName") != EXCEPTION_OWNERS[expected]:
                    unauthorized_exception.append(f"{kind}/{name}")
            elif collection.get("items"):
                unauthorized_exception.append(f"{kind}/{name}")

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "cluster": {
            "kube_system_uid": client.raw("/api/v1/namespaces/kube-system")
            .get("metadata", {})
            .get("uid"),
        },
        "scientific_namespaces": list(SCIENTIFIC_NAMESPACES),
        "inspected_namespaces": list(BASELINE_NAMESPACES),
        "objects": sorted(objects, key=lambda item: (item["namespace"], item["kind"], item["name"])),
        "reference_host_paths": reference_host_paths,
        "baseline_incompatible_objects": incompatible_objects,
        "unauthorized_exception_objects": sorted(unauthorized_exception),
    }
    result["inventory_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--kubeconfig", required=True, type=Path)
    result.add_argument("--context", required=True)
    result.add_argument("--require-clean", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        result = inventory(Kubectl(args.kubeconfig, args.context))
        if args.require_clean and (
            result["reference_host_paths"] != 0
            or result["baseline_incompatible_objects"] != 0
            or result["unauthorized_exception_objects"]
        ):
            raise InventoryError("pre-enforcement inventory is not clean")
    except (InventoryError, OSError, json.JSONDecodeError) as error:
        print(f"SAI-07 inventory refused: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
