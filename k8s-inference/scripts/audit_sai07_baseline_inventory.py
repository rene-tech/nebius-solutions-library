#!/usr/bin/env python3
"""Produce and verify a canonical pre-enforcement workload inventory.

The first read becomes a private frozen baseline artifact.  Every later gate
must read that exact artifact through a descriptor-fenced path and bind its
digest into the signed rollout context.  ``--mode initial`` proves that the
live cluster still equals the artifact object-for-object; ``--mode clean``
proves the same cluster and namespace inventory now have no Baseline or
restricted/root/AppArmor exceptions.  This tool is read-only and is not
rollout authority by itself.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = "fs2-serve.nebius.ai/sai07-baseline-inventory/v2"
VERIFICATION_SCHEMA = "fs2-serve.nebius.ai/sai07-baseline-verification/v1"
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
    "Pod": ("v1", "/api/v1/namespaces/{namespace}/pods", ("spec",)),
    "ReplicationController": (
        "v1",
        "/api/v1/namespaces/{namespace}/replicationcontrollers",
        ("spec", "template", "spec"),
    ),
    "Deployment": (
        "apps/v1",
        "/apis/apps/v1/namespaces/{namespace}/deployments",
        ("spec", "template", "spec"),
    ),
    "StatefulSet": (
        "apps/v1",
        "/apis/apps/v1/namespaces/{namespace}/statefulsets",
        ("spec", "template", "spec"),
    ),
    "DaemonSet": (
        "apps/v1",
        "/apis/apps/v1/namespaces/{namespace}/daemonsets",
        ("spec", "template", "spec"),
    ),
    "ReplicaSet": (
        "apps/v1",
        "/apis/apps/v1/namespaces/{namespace}/replicasets",
        ("spec", "template", "spec"),
    ),
    "Job": ("batch/v1", "/apis/batch/v1/namespaces/{namespace}/jobs", ("spec", "template", "spec")),
    "CronJob": (
        "batch/v1",
        "/apis/batch/v1/namespaces/{namespace}/cronjobs",
        ("spec", "jobTemplate", "spec", "template", "spec"),
    ),
    "JobSet": (
        "jobset.x-k8s.io/v1alpha2",
        "/apis/jobset.x-k8s.io/v1alpha2/namespaces/{namespace}/jobsets",
        (),
    ),
    "ModelDeployment": (
        "inference.fs2.nebius.ai/v1alpha1",
        "/apis/inference.fs2.nebius.ai/v1alpha1/namespaces/{namespace}/modeldeployments",
        (),
    ),
    "ScaledObject": (
        "keda.sh/v1alpha1",
        "/apis/keda.sh/v1alpha1/namespaces/{namespace}/scaledobjects",
        (),
    ),
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

    def raw(self, uri: str, *, optional: bool = False) -> dict[str, Any]:
        result = subprocess.run(
            [*self.base, "get", "--raw", uri], check=False, capture_output=True, text=True
        )
        if result.returncode != 0 and optional and ("not found" in result.stderr.lower() or "404" in result.stderr):
            return {"apiVersion": "v1", "kind": "List", "items": [], "metadata": {"resourceVersion": "0"}}
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


def pod_findings(spec: dict[str, Any], annotations: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    findings: set[str] = set()
    restricted: set[str] = set()
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
            restricted.add("privileged")
        if security.get("runAsUser") == 0 or security.get("runAsNonRoot") is False:
            restricted.add("root")
        if security.get("runAsNonRoot") is not True and (spec.get("securityContext") or {}).get("runAsNonRoot") is not True:
            restricted.add("runAsNonRoot")
        if security.get("allowPrivilegeEscalation") is not False:
            restricted.add("allowPrivilegeEscalation")
        if security.get("procMount") not in (None, "Default"):
            findings.add("procMount")
        seccomp = security.get("seccompProfile", {})
        if isinstance(seccomp, dict) and seccomp.get("type") == "Unconfined":
            findings.add("unconfinedSeccomp")
            restricted.add("unconfinedSeccomp")
        if not seccomp and not (spec.get("securityContext") or {}).get("seccompProfile"):
            restricted.add("seccompProfile")
        apparmor = security.get("appArmorProfile", {})
        if isinstance(apparmor, dict) and apparmor.get("type") == "Unconfined":
            findings.add("unconfinedAppArmor")
            restricted.add("unconfinedAppArmor")
        added = set((security.get("capabilities", {}) or {}).get("add", []) or [])
        if not added.issubset(BASELINE_CAPABILITIES):
            findings.add("capabilities")
        if added:
            restricted.add("capabilities")
        dropped = set((security.get("capabilities", {}) or {}).get("drop", []) or [])
        if "ALL" not in dropped:
            restricted.add("capabilitiesDrop")
        for port in container.get("ports", []) if isinstance(container, dict) else []:
            if isinstance(port, dict) and int(port.get("hostPort", 0) or 0) != 0:
                findings.add("hostPort")
    pod_security = spec.get("securityContext", {}) or {}
    if pod_security.get("runAsUser") == 0 or pod_security.get("runAsNonRoot") is False:
        restricted.add("root")
    if pod_security.get("runAsNonRoot") is not True:
        restricted.add("runAsNonRoot")
    seccomp = pod_security.get("seccompProfile", {}) if isinstance(pod_security, dict) else {}
    if isinstance(seccomp, dict) and seccomp.get("type") == "Unconfined":
        findings.add("unconfinedSeccomp")
        restricted.add("unconfinedSeccomp")
    if not seccomp:
        restricted.add("seccompProfile")
    for key, value in (annotations or {}).items():
        if key.startswith("container.apparmor.security.beta.kubernetes.io/") and value == "unconfined":
            findings.add("unconfinedAppArmor")
            restricted.add("unconfinedAppArmor")
    return sorted(findings), sorted(restricted)


def baseline_findings(spec: dict[str, Any]) -> list[str]:
    """Backward-compatible focused helper used by contract tests."""

    return pod_findings(spec)[0]


def read_regular(path: Path, limit: int = 128 * 1024 * 1024) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise InventoryError(f"cannot safely open baseline artifact: {error.strerror}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise InventoryError("baseline artifact must be a bounded regular file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise InventoryError("baseline artifact changed while it was read")
        return payload
    finally:
        os.close(descriptor)


def validate_artifact(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise InventoryError("baseline artifact schema is unsupported")
    required = {
        "schema",
        "captured_at",
        "cluster",
        "scientific_namespaces",
        "inspected_namespaces",
        "collections",
        "objects",
        "reference_host_paths",
        "baseline_incompatible_objects",
        "restricted_incompatible_objects",
        "unauthorized_exception_objects",
        "inventory_sha256",
    }
    if set(value) != required:
        raise InventoryError("baseline artifact fields differ from the canonical schema")
    unsigned = dict(value)
    digest = unsigned.pop("inventory_sha256")
    if digest != hashlib.sha256(canonical(unsigned)).hexdigest():
        raise InventoryError("baseline artifact self-digest differs")
    if value["scientific_namespaces"] != list(SCIENTIFIC_NAMESPACES):
        raise InventoryError("baseline artifact scientific namespace inventory differs")
    if value["inspected_namespaces"] != list(BASELINE_NAMESPACES):
        raise InventoryError("baseline artifact inspected namespace inventory differs")
    if not isinstance(value["objects"], list) or not isinstance(value["collections"], list):
        raise InventoryError("baseline artifact inventories must be lists")
    return value


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
    collections: list[dict[str, Any]] = []
    reference_host_paths = 0
    incompatible_objects = 0
    restricted_incompatible_objects = 0
    for namespace in BASELINE_NAMESPACES:
        for kind, (api_version, uri, path) in COLLECTIONS.items():
            collection = client.raw(uri.format(namespace=namespace), optional=kind in {"JobSet", "ModelDeployment", "ScaledObject"})
            items = collection.get("items", [])
            if not isinstance(items, list):
                raise InventoryError(f"{api_version}/{kind} collection is malformed")
            collection_metadata = collection.get("metadata", {})
            collections.append(
                {
                    "api_version": api_version,
                    "kind": kind,
                    "namespace": namespace,
                    "resource_version": str(collection_metadata.get("resourceVersion", "")),
                    "item_count": len(items),
                }
            )
            for item in items:
                spec = nested(item, path)
                metadata = item.get("metadata", {})
                annotations = metadata.get("annotations", {}) if isinstance(metadata, dict) else {}
                findings, restricted_findings = pod_findings(spec, annotations) if path else ([], [])
                if findings:
                    incompatible_objects += 1
                if restricted_findings:
                    restricted_incompatible_objects += 1
                if "hostPath" in findings:
                    reference_host_paths += 1
                projection = {
                    "apiVersion": item.get("apiVersion", api_version),
                    "kind": item.get("kind", kind),
                    "metadata": {
                        "name": metadata.get("name"),
                        "namespace": metadata.get("namespace", namespace),
                        "uid": metadata.get("uid"),
                        "resourceVersion": metadata.get("resourceVersion"),
                        "generation": metadata.get("generation"),
                        "ownerReferences": metadata.get("ownerReferences", []),
                    },
                    "spec": item.get("spec", {}),
                }
                objects.append(
                    {
                        "api_version": api_version,
                        "kind": kind,
                        "namespace": namespace,
                        "name": metadata.get("name"),
                        "uid": metadata.get("uid"),
                        "resource_version": metadata.get("resourceVersion"),
                        "object_sha256": hashlib.sha256(canonical(projection)).hexdigest(),
                        "pod_spec_sha256": hashlib.sha256(canonical(spec)).hexdigest(),
                        "baseline_findings": findings,
                        "restricted_findings": restricted_findings,
                    }
                )

    unauthorized_exception: list[str] = []
    for kind, (_, uri, path) in COLLECTIONS.items():
        collection = client.raw(uri.format(namespace=EXCEPTION_NAMESPACE), optional=kind in {"JobSet", "ModelDeployment", "ScaledObject"})
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
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "cluster": {
            "kube_system_uid": client.raw("/api/v1/namespaces/kube-system")
            .get("metadata", {})
            .get("uid"),
        },
        "scientific_namespaces": list(SCIENTIFIC_NAMESPACES),
        "inspected_namespaces": list(BASELINE_NAMESPACES),
        "collections": sorted(collections, key=lambda item: (item["namespace"], item["api_version"], item["kind"])),
        "objects": sorted(objects, key=lambda item: (item["namespace"], item["api_version"], item["kind"], item["name"])),
        "reference_host_paths": reference_host_paths,
        "baseline_incompatible_objects": incompatible_objects,
        "restricted_incompatible_objects": restricted_incompatible_objects,
        "unauthorized_exception_objects": sorted(unauthorized_exception),
    }
    result["inventory_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def verify_against_artifact(
    live: dict[str, Any], artifact_bytes: bytes, expected_sha256: str, mode: str
) -> dict[str, Any]:
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    if artifact_sha256 != expected_sha256:
        raise InventoryError("baseline artifact bytes differ from the reviewed SHA-256")
    try:
        artifact = validate_artifact(json.loads(artifact_bytes))
    except json.JSONDecodeError as error:
        raise InventoryError("baseline artifact is not JSON") from error
    if live["cluster"] != artifact["cluster"]:
        raise InventoryError("live cluster identity differs from the frozen baseline artifact")
    if mode == "initial":
        comparable = (
            "scientific_namespaces",
            "inspected_namespaces",
            "collections",
            "objects",
            "reference_host_paths",
            "baseline_incompatible_objects",
            "restricted_incompatible_objects",
            "unauthorized_exception_objects",
        )
        if any(live[field] != artifact[field] for field in comparable):
            raise InventoryError("live initial inventory differs from the frozen baseline artifact")
    elif mode == "clean":
        if (
            live["reference_host_paths"] != 0
            or live["baseline_incompatible_objects"] != 0
            or live["restricted_incompatible_objects"] != 0
            or live["unauthorized_exception_objects"]
        ):
            raise InventoryError("live pre-enforcement inventory is not clean")
    else:  # pragma: no cover - argparse constrains this boundary
        raise InventoryError("unsupported verification mode")
    return {
        "schema": VERIFICATION_SCHEMA,
        "mode": mode,
        "baseline_artifact_sha256": artifact_sha256,
        "baseline_inventory_sha256": artifact["inventory_sha256"],
        "baseline_reference_host_paths": artifact["reference_host_paths"],
        "baseline_incompatible_objects": artifact["baseline_incompatible_objects"],
        "baseline_restricted_incompatible_objects": artifact["restricted_incompatible_objects"],
        "live_inventory_sha256": live["inventory_sha256"],
        "live_reference_host_paths": live["reference_host_paths"],
        "live_baseline_incompatible_objects": live["baseline_incompatible_objects"],
        "live_restricted_incompatible_objects": live["restricted_incompatible_objects"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--kubeconfig", required=True, type=Path)
    result.add_argument("--context", required=True)
    result.add_argument("--baseline-artifact", type=Path)
    result.add_argument("--baseline-sha256")
    result.add_argument("--mode", choices=("capture", "initial", "clean"), default="capture")
    result.add_argument("--require-clean", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        live = inventory(Kubectl(args.kubeconfig, args.context))
        if args.mode == "capture":
            if args.baseline_artifact is not None or args.baseline_sha256 is not None:
                raise InventoryError("capture mode does not accept a baseline artifact")
            result = live
        else:
            if args.baseline_artifact is None or args.baseline_sha256 is None:
                raise InventoryError("initial/clean verification requires the exact baseline artifact and SHA-256")
            if len(args.baseline_sha256) != 64 or any(character not in "0123456789abcdef" for character in args.baseline_sha256):
                raise InventoryError("baseline SHA-256 is malformed")
            result = verify_against_artifact(
                live,
                read_regular(args.baseline_artifact),
                args.baseline_sha256,
                args.mode,
            )
        if args.require_clean and (
            live["reference_host_paths"] != 0
            or live["baseline_incompatible_objects"] != 0
            or live["restricted_incompatible_objects"] != 0
            or live["unauthorized_exception_objects"]
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
