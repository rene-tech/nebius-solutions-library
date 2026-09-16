#!/usr/bin/env python3
"""Authenticate, live-verify, and consume one SAI-07 rollout transition.

The v4 signature covers the complete canonical bundle: authority, deployment
context, transition, expiry, and every live observation. The owner consumer
re-reads every named object and collection immediately before atomically
advancing a Kubernetes ConfigMap ledger with a resourceVersion compare-and-swap.
The downstream consumer may consume that exact phase authorization once. A
plan-time signature check is deliberately insufficient and is not supported.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode

BUNDLE_SCHEMA = "fs2-serve.nebius.ai/pod-security-rollout-receipt/v4"
LEDGER_SCHEMA = "fs2-serve.nebius.ai/pod-security-rollout-ledger/v2"
LEDGER_DATA_KEYS = {
    "schema",
    "context_sha256",
    "authority_key_id",
    "authority_signer_identity",
    "authority_public_key_sha256",
    "sequence",
    "state",
    "last_bundle_sha256",
    "last_receipt_id",
    "last_nonce",
    "authorization_phase",
    "authorization_bundle_sha256",
    "authorization_nonce",
    "authorization_owner_acknowledged",
    "authorization_downstream_acknowledged",
}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9](?:[-A-Za-z0-9._:@/]{0,251}[A-Za-z0-9])?$")
DNS_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
MAX_BUNDLE_AGE = dt.timedelta(minutes=15)
MAX_OBSERVATION_AGE = dt.timedelta(minutes=2)
MAX_CLOCK_SKEW = dt.timedelta(seconds=30)
MAX_OBJECTS = 2048
MAX_INVENTORIES = 256
EXPECTED_REFERENCE_HOST_PATHS = 103
EXPECTED_BASELINE_INCOMPATIBLE_OBJECTS = 103
EXPECTED_RESTRICTED_INCOMPATIBLE_OBJECTS = 716

PHASE_TRANSITIONS = {
    "bootstrap-baseline": ("unmanaged", "baseline-captured"),
    "migrate-reference-data": ("baseline-captured", "exception-ready"),
    "cleanup-legacy-resources": ("exception-ready", "reference-data-ready"),
    "quiesce-enforcement": ("reference-data-ready", "enforcement-quiesced"),
    "enforce": ("enforcement-quiesced", "baseline-enforced"),
    "rollback-remove-enforcement": ("baseline-enforced", "enforcement-removed"),
    "rollback-restore-host-agents": ("enforcement-removed", "host-agents-restored"),
    "rollback-remove-exception": ("host-agents-restored", "rolled-back"),
}
OBSERVATION_STATES = {
    "bootstrap-baseline": "baseline-captured",
    "migrate-reference-data": "exception-ready",
    "cleanup-legacy-resources": "reference-data-ready",
    "quiesce-enforcement": "cleanup-complete",
    "enforce": "enforcement-quiesced",
    "rollback-remove-enforcement": "baseline-enforced",
    "rollback-restore-host-agents": "enforcement-removed",
    "rollback-remove-exception": "host-agents-restored",
}

RESOURCE_PATHS = {
    ("v1", "ConfigMap"): "configmaps",
    ("v1", "Namespace"): "namespaces",
    ("v1", "PersistentVolumeClaim"): "persistentvolumeclaims",
    ("v1", "PersistentVolume"): "persistentvolumes",
    ("v1", "Pod"): "pods",
    ("v1", "PodTemplate"): "podtemplates",
    ("v1", "ReplicationController"): "replicationcontrollers",
    ("v1", "ServiceAccount"): "serviceaccounts",
    ("apps/v1", "DaemonSet"): "daemonsets",
    ("apps/v1", "Deployment"): "deployments",
    ("apps/v1", "ReplicaSet"): "replicasets",
    ("apps/v1", "StatefulSet"): "statefulsets",
    ("batch/v1", "CronJob"): "cronjobs",
    ("batch/v1", "Job"): "jobs",
    ("networking.k8s.io/v1", "NetworkPolicy"): "networkpolicies",
    ("rbac.authorization.k8s.io/v1", "Role"): "roles",
    ("rbac.authorization.k8s.io/v1", "RoleBinding"): "rolebindings",
    ("storage.k8s.io/v1", "StorageClass"): "storageclasses",
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"): "validatingadmissionpolicies",
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"): "validatingadmissionpolicybindings",
    ("jobset.x-k8s.io/v1alpha2", "JobSet"): "jobsets",
    ("inference.fs2.nebius.ai/v1alpha1", "ModelDeployment"): "modeldeployments",
    ("keda.sh/v1alpha1", "ScaledObject"): "scaledobjects",
}

CLUSTER_SCOPED = {
    ("v1", "Namespace"),
    ("v1", "PersistentVolume"),
    ("storage.k8s.io/v1", "StorageClass"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"),
}

BASELINE_INVENTORY_KINDS = {
    ("v1", "ConfigMap"),
    ("v1", "Pod"),
    ("v1", "PodTemplate"),
    ("v1", "ServiceAccount"),
    ("v1", "ReplicationController"),
    ("apps/v1", "Deployment"),
    ("apps/v1", "ReplicaSet"),
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
    ("batch/v1", "Job"),
    ("batch/v1", "CronJob"),
    ("jobset.x-k8s.io/v1alpha2", "JobSet"),
    ("inference.fs2.nebius.ai/v1alpha1", "ModelDeployment"),
    ("keda.sh/v1alpha1", "ScaledObject"),
    ("networking.k8s.io/v1", "NetworkPolicy"),
}
LEGACY_BASELINE_INVENTORY_KINDS = BASELINE_INVENTORY_KINDS - {("v1", "ConfigMap")}

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

EXCEPTION_DAEMONSETS = {
    "fs2-dcgm-exporter",
    "fs2-node-exporter",
    "fs2-otel-node-agent",
    "fs2-serve-control-plane-gpu-observer",
}
EXCEPTION_SERVICE_ACCOUNTS = {
    "fs2-dcgm-exporter",
    "fs2-node-exporter",
    "fs2-otel-node",
    "fs2-serve-control-plane-gpu-observer",
}
SNAPSHOT_EXCEPTION_OBJECTS = {
    ("v1", "Namespace", "", "fs2-snapshot-operations"),
    ("v1", "ServiceAccount", "fs2-snapshot-operations", "fs2-snapshot-runtime"),
    ("v1", "ServiceAccount", "fs2-system", "fs2-snapshot-manager"),
    ("rbac.authorization.k8s.io/v1", "Role", "fs2-snapshot-operations", "fs2-snapshot-manager"),
    (
        "rbac.authorization.k8s.io/v1",
        "RoleBinding",
        "fs2-snapshot-operations",
        "fs2-snapshot-manager",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-snapshot-exact-profile",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-snapshot-exact-profile",
    ),
    (
        "networking.k8s.io/v1",
        "NetworkPolicy",
        "fs2-snapshot-operations",
        "fs2-snapshot-default-deny",
    ),
}
BIOIR_REFERENCE_NAMESPACES = (
    "fs2-bioir-boltz2",
    "fs2-bioir-coverage",
    "fs2-bioir-openfold",
    "fs2-bioir-protenix",
    "fs2-bioir-snapshot",
)
REFERENCE_SUCCESSOR_CLAIMS = (
    *((namespace, "fs2-reference-data-rwx") for namespace in BIOIR_REFERENCE_NAMESPACES),
    ("fs2-snapshot-operations", "fs2-snapshot-reference"),
)
REFERENCE_SUCCESSOR_VOLUMES = {
    ("fs2-bioir-boltz2", "fs2-reference-data-rwx"): "fs2-sai07-ref-bioir-boltz2",
    ("fs2-bioir-coverage", "fs2-reference-data-rwx"): "fs2-sai07-ref-bioir-coverage",
    ("fs2-bioir-openfold", "fs2-reference-data-rwx"): "fs2-sai07-ref-bioir-openfold",
    ("fs2-bioir-protenix", "fs2-reference-data-rwx"): "fs2-sai07-ref-bioir-protenix",
    ("fs2-bioir-snapshot", "fs2-reference-data-rwx"): "fs2-sai07-ref-bioir-snapshot",
    ("fs2-snapshot-operations", "fs2-snapshot-reference"): "fs2-sai07-ref-snapshot-operations",
}
REFERENCE_SUCCESSOR_STORAGE_CLASS = "fs2-reference-data-retained-sc"
SNAPSHOT_CHECKPOINT_CLAIM = ("fs2-snapshot-operations", "fs2-snapshot-checkpoints")
SNAPSHOT_CHECKPOINT_VOLUME = "fs2-sai07-snapshot-checkpoints"
SNAPSHOT_CHECKPOINT_STORAGE_CLASS = "fs2-snapshot-checkpoints-retained-sc"


class ReceiptError(ValueError):
    """The bundle, live state, or ledger cannot authorize this transition."""


class ConflictError(ReceiptError):
    """The durable ledger changed during compare-and-swap."""


class KubeClient(Protocol):
    def get_object(self, api_version: str, kind: str, namespace: str, name: str) -> dict[str, Any] | None: ...

    def list_collection(
        self,
        api_version: str,
        kind: str,
        namespace: str,
        label_selector: str,
        field_selector: str,
    ) -> dict[str, Any]: ...

    def replace_config_map(self, value: dict[str, Any]) -> dict[str, Any]: ...


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReceiptError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReceiptError(f"{label} must be an integer >= {minimum}")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ReceiptError(f"{label} fields differ from the canonical schema")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _instant(value: Any, label: str) -> dt.datetime:
    raw = _string(value, label)
    if not raw.endswith("Z"):
        raise ReceiptError(f"{label} must be an RFC3339 UTC instant")
    try:
        parsed = dt.datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as error:
        raise ReceiptError(f"{label} must be an RFC3339 UTC instant") from error
    if parsed.tzinfo != dt.UTC:
        raise ReceiptError(f"{label} must use UTC")
    return parsed


def _read_regular_file(path: Path, label: str, limit: int = 1_048_576) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ReceiptError(f"cannot safely open {label}: {error.strerror}") from error
    try:
        stat = os.fstat(descriptor)
        if not stat.st_mode & 0o100000:
            raise ReceiptError(f"{label} is not a regular file")
        if stat.st_size > limit:
            raise ReceiptError(f"{label} exceeds the size limit")
        payload = b""
        while len(payload) < stat.st_size:
            chunk = os.read(descriptor, min(65536, stat.st_size - len(payload)))
            if not chunk:
                break
            payload += chunk
    finally:
        os.close(descriptor)
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return _object(json.loads(_read_regular_file(path, path.name)), "receipt bundle")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError(f"{path.name} is not JSON") from error


def _verify_signature(bundle: dict[str, Any], public_key_bytes: bytes, expected_key_id: str) -> None:
    signature = _object(bundle.get("signature"), "signature")
    _exact_keys(signature, {"algorithm", "key_id", "value"}, "signature")
    if signature["algorithm"] != "ed25519" or signature["key_id"] != expected_key_id:
        raise ReceiptError("bundle signature authority differs")
    try:
        signature_bytes = base64.b64decode(signature["value"], validate=True)
    except (binascii.Error, TypeError) as error:
        raise ReceiptError("bundle signature is not strict base64") from error
    if len(signature_bytes) != 64:
        raise ReceiptError("Ed25519 signature must be exactly 64 bytes")

    unsigned = dict(bundle)
    del unsigned["signature"]
    with tempfile.TemporaryDirectory(prefix="fs2-psa-receipt-") as directory:
        root = Path(directory)
        message_path = root / "bundle.json"
        signature_path = root / "signature.bin"
        public_key_path = root / "public-key.pem"
        message_path.write_bytes(_canonical(unsigned))
        signature_path.write_bytes(signature_bytes)
        # Verify the same descriptor-fenced bytes whose digest was checked.
        # Reopening the operator-supplied path here would permit a pathname or
        # symlink swap between digest and signature verification.
        public_key_path.write_bytes(public_key_bytes)
        os.chmod(message_path, 0o600)
        os.chmod(signature_path, 0o600)
        os.chmod(public_key_path, 0o600)
        completed = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_key_path),
                "-rawin",
                "-in",
                str(message_path),
                "-sigfile",
                str(signature_path),
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    if completed.returncode != 0:
        raise ReceiptError("whole-bundle signature verification failed")


def _validate_context(context: dict[str, Any], expected: dict[str, Any]) -> None:
    _exact_keys(
        context,
        {
            "cluster_id",
            "run_id",
            "kube_system_uid",
            "deployment_nonce",
            "exception_admission_sha256",
            "psa_version",
            "scientific_namespaces",
            "pvc",
            "dataset",
            "storage",
            "storage_evidence",
            "successor_storage_sha256",
            "successor_storage",
            "baseline",
            "host_agents",
            "host_agent_configs",
        },
        "context",
    )
    if context != expected:
        raise ReceiptError("signed context differs from the exact deployment context")
    for field in ("cluster_id", "run_id", "kube_system_uid", "deployment_nonce"):
        if not IDENTIFIER_RE.fullmatch(_string(context[field], f"context.{field}")):
            raise ReceiptError(f"context.{field} is malformed")
    if not SHA256_RE.fullmatch(_string(context["exception_admission_sha256"], "context.exception_admission_sha256")):
        raise ReceiptError("context.exception_admission_sha256 is malformed")
    if not re.fullmatch(r"v1\.[0-9]{1,2}", _string(context["psa_version"], "context.psa_version")):
        raise ReceiptError("context.psa_version must pin one Kubernetes minor")
    namespaces = context["scientific_namespaces"]
    if (
        not isinstance(namespaces, list)
        or not namespaces
        or namespaces != sorted(set(namespaces))
        or not all(isinstance(item, str) and DNS_RE.fullmatch(item) for item in namespaces)
    ):
        raise ReceiptError("scientific namespace inventory must be non-empty, sorted, and unique")

    host_agents = context["host_agents"]
    if not isinstance(host_agents, list) or len(host_agents) != 4:
        raise ReceiptError("context.host_agents must describe exactly four migrations")
    components: list[str] = []
    for index, item_raw in enumerate(host_agents):
        item = _object(item_raw, f"context.host_agents[{index}]")
        _exact_keys(item, {"component", "legacy", "exception"}, f"context.host_agents[{index}]")
        component = _string(item["component"], f"context.host_agents[{index}].component")
        components.append(component)
        for location in ("legacy", "exception"):
            identity = _object(item[location], f"context.host_agents[{index}].{location}")
            _exact_keys(identity, {"namespace", "name"}, f"context.host_agents[{index}].{location}")
            if not all(
                DNS_RE.fullmatch(_string(identity[field], f"context.host_agents[{index}].{location}.{field}"))
                for field in ("namespace", "name")
            ):
                raise ReceiptError("host-agent identities must be DNS labels")
        if item["exception"]["namespace"] != "fs2-node-observability":
            raise ReceiptError("every exception host agent must use fs2-node-observability")
    if sorted(components) != ["dcgm-exporter", "gpu-observer", "node-exporter", "otel-node"]:
        raise ReceiptError("context.host_agents component inventory differs")

    host_agent_configs = context["host_agent_configs"]
    if not isinstance(host_agent_configs, list) or len(host_agent_configs) != 3:
        raise ReceiptError("context.host_agent_configs must bind exactly three immutable configs")
    config_components: list[str] = []
    config_names: list[str] = []
    for index, item_raw in enumerate(host_agent_configs):
        item = _object(item_raw, f"context.host_agent_configs[{index}]")
        _exact_keys(
            item,
            {"component", "namespace", "name", "data_sha256"},
            f"context.host_agent_configs[{index}]",
        )
        config_components.append(_string(item["component"], "host-agent config component"))
        if item["namespace"] != "fs2-node-observability":
            raise ReceiptError("host-agent configs must use the exception namespace")
        config_names.append(_string(item["name"], "host-agent config name"))
        if not DNS_RE.fullmatch(item["name"]):
            raise ReceiptError("host-agent config name is malformed")
        if not SHA256_RE.fullmatch(_string(item["data_sha256"], "host-agent config digest")):
            raise ReceiptError("host-agent config data digest is malformed")
    if config_components != sorted(config_components) or len(set(config_names)) != 3:
        raise ReceiptError("host-agent configs must be sorted and unique")
    if config_components != ["dcgm-cold-config", "dcgm-metrics-config", "otel-node-config"]:
        raise ReceiptError("host-agent config component inventory differs")

    pvc = _object(context["pvc"], "context.pvc")
    _exact_keys(
        pvc,
        {"namespace", "name", "uid", "resource_version", "volume_name", "storage_class"},
        "context.pvc",
    )
    if pvc["namespace"] != "fs2-reference-data" or pvc["name"] != "fs2-reference-data-rwx":
        raise ReceiptError("receipt must bind the canonical reference-data claim")
    if not IDENTIFIER_RE.fullmatch(_string(pvc["uid"], "context.pvc.uid")):
        raise ReceiptError("context.pvc.uid is malformed")
    for field in ("resource_version", "volume_name"):
        if not IDENTIFIER_RE.fullmatch(_string(pvc[field], f"context.pvc.{field}")):
            raise ReceiptError(f"context.pvc.{field} is malformed")
    if pvc["storage_class"] != "fs2-reference-data-retained-sc":
        raise ReceiptError("receipt must bind the dedicated retained StorageClass")

    dataset = _object(context["dataset"], "context.dataset")
    _exact_keys(dataset, {"id", "revision", "tree_sha256"}, "context.dataset")
    _string(dataset["id"], "context.dataset.id")
    _string(dataset["revision"], "context.dataset.revision")
    if not SHA256_RE.fullmatch(_string(dataset["tree_sha256"], "context.dataset.tree_sha256")):
        raise ReceiptError("context.dataset.tree_sha256 is malformed")

    storage = _object(context["storage"], "context.storage")
    _exact_keys(
        storage,
        {"filesystem_id", "capacity_gib", "claim_size_gib", "forbid_deletion", "retention_mode"},
        "context.storage",
    )
    _string(storage["filesystem_id"], "context.storage.filesystem_id")
    capacity = _integer(storage["capacity_gib"], "context.storage.capacity_gib", 1611)
    claim_size = _integer(storage["claim_size_gib"], "context.storage.claim_size_gib", 1611)
    if claim_size > capacity:
        raise ReceiptError("claim size exceeds retained filesystem capacity")
    if storage["forbid_deletion"] is not True or storage["retention_mode"] != "retain":
        raise ReceiptError("reference-data storage is not durably retained")

    storage_evidence = _object(context["storage_evidence"], "context.storage_evidence")
    _exact_keys(
        storage_evidence,
        {
            "read_proof_schema",
            "checkpoint_proof_schema",
            "probe_image",
            "tools_config_map",
            "tools_data_sha256",
        },
        "context.storage_evidence",
    )
    if storage_evidence["read_proof_schema"] != "fs2-serve.nebius.ai/reference-data-csi-readiness/v2":
        raise ReceiptError("context.storage_evidence.read_proof_schema is unsupported")
    if storage_evidence["checkpoint_proof_schema"] != "fs2-serve.nebius.ai/checkpoint-durability-proof/v1":
        raise ReceiptError("context.storage_evidence.checkpoint_proof_schema is unsupported")
    if not re.fullmatch(
        r"[^@\s]+@sha256:[a-f0-9]{64}",
        _string(storage_evidence["probe_image"], "context.storage_evidence.probe_image"),
    ):
        raise ReceiptError("context.storage_evidence.probe_image must be digest pinned")
    if not DNS_RE.fullmatch(_string(storage_evidence["tools_config_map"], "context storage tools ConfigMap")):
        raise ReceiptError("context.storage_evidence.tools_config_map is malformed")
    if not SHA256_RE.fullmatch(_string(storage_evidence["tools_data_sha256"], "context storage tools digest")):
        raise ReceiptError("context.storage_evidence.tools_data_sha256 is malformed")
    if not SHA256_RE.fullmatch(
        _string(context["successor_storage_sha256"], "context.successor_storage_sha256")
    ) or context["successor_storage_sha256"] == "0" * 64:
        raise ReceiptError("context.successor_storage_sha256 must bind a non-empty custody contract")
    successor_storage = _object(context["successor_storage"], "context.successor_storage")
    if _sha256(_canonical(successor_storage)) != context["successor_storage_sha256"]:
        raise ReceiptError("context successor-storage custody contract digest differs")
    _exact_keys(
        successor_storage,
        {"schema", "reference_source", "checkpoint_source"},
        "context.successor_storage",
    )
    if successor_storage["schema"] != "fs2-serve.nebius.ai/sai07-successor-storage/v1":
        raise ReceiptError("context successor-storage schema is unsupported")
    reference_source = _object(successor_storage["reference_source"], "successor reference source")
    _exact_keys(
        reference_source,
        {
            "persistent_volume_name",
            "uid",
            "resource_version",
            "csi_driver",
            "volume_handle",
            "volume_attributes",
            "capacity_quantity",
            "capacity_gib",
            "provisioning_receipt_sha256",
            "storage_owner",
        },
        "successor reference source",
    )
    checkpoint_source = _object(successor_storage["checkpoint_source"], "successor checkpoint source")
    _exact_keys(
        checkpoint_source,
        {
            "persistent_volume_name",
            "csi_driver",
            "volume_handle",
            "volume_attributes",
            "capacity_gib",
            "requested_gib",
            "provisioning_receipt_sha256",
            "storage_owner",
        },
        "successor checkpoint source",
    )
    if (
        reference_source["csi_driver"] != "reference-data.mounted-fs-path.csi.nebius.ai"
        or checkpoint_source["csi_driver"] != reference_source["csi_driver"]
        or checkpoint_source["persistent_volume_name"] != SNAPSHOT_CHECKPOINT_VOLUME
        or checkpoint_source["volume_handle"] == reference_source["volume_handle"]
        or not re.fullmatch(r"[1-9][0-9]*(?:Ki|Mi|Gi|Ti)", str(reference_source["capacity_quantity"]))
        or reference_source["capacity_quantity"] != f"{reference_source['capacity_gib']}Gi"
        or _integer(reference_source["capacity_gib"], "successor reference capacity", 1611) > capacity
        or _integer(checkpoint_source["requested_gib"], "checkpoint requested capacity", 1)
        > _integer(checkpoint_source["capacity_gib"], "checkpoint capacity", 1)
        or 1611 + int(checkpoint_source["capacity_gib"]) > capacity
    ):
        raise ReceiptError("successor storage is not the exact retained, distinct CSI contract")
    for label, source in (("reference", reference_source), ("checkpoint", checkpoint_source)):
        if (
            not IDENTIFIER_RE.fullmatch(_string(source["persistent_volume_name"], f"{label} PV name"))
            or not IDENTIFIER_RE.fullmatch(_string(source["volume_handle"], f"{label} CSI handle"))
            or not SHA256_RE.fullmatch(
                _string(source["provisioning_receipt_sha256"], f"{label} provisioning receipt")
            )
            or not IDENTIFIER_RE.fullmatch(_string(source["storage_owner"], f"{label} storage owner"))
            or not isinstance(source["volume_attributes"], dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in source["volume_attributes"].items())
        ):
            raise ReceiptError(f"successor {label} custody fields are malformed")
    for field in ("uid", "resource_version"):
        if not IDENTIFIER_RE.fullmatch(_string(reference_source[field], f"successor reference {field}")):
            raise ReceiptError(f"successor reference {field} is malformed")

    baseline = _object(context["baseline"], "context.baseline")
    _exact_keys(
        baseline,
        {
            "schema",
            "artifact_sha256",
            "inventory_sha256",
            "reference_host_paths",
            "baseline_incompatible_objects",
            "restricted_incompatible_objects",
        },
        "context.baseline",
    )
    if baseline["schema"] not in {
        "fs2-serve.nebius.ai/sai07-baseline-inventory/v3",
        "fs2-serve.nebius.ai/sai07-baseline-inventory/v4",
    }:
        raise ReceiptError("context.baseline.schema is unsupported")
    for field in ("artifact_sha256", "inventory_sha256"):
        if not SHA256_RE.fullmatch(_string(baseline[field], f"context.baseline.{field}")):
            raise ReceiptError(f"context.baseline.{field} is malformed")
    for field in (
        "reference_host_paths",
        "baseline_incompatible_objects",
        "restricted_incompatible_objects",
    ):
        _integer(baseline[field], f"context.baseline.{field}")
    if baseline["schema"].endswith("/v3") and (
        baseline["reference_host_paths"] != EXPECTED_REFERENCE_HOST_PATHS
        or baseline["baseline_incompatible_objects"] != EXPECTED_BASELINE_INCOMPATIBLE_OBJECTS
        or baseline["restricted_incompatible_objects"] != EXPECTED_RESTRICTED_INCOMPATIBLE_OBJECTS
    ):
        raise ReceiptError("legacy signed v3 baseline differs from preserved 103/103/716 evidence")


def _validate_baseline_artifact(payload: bytes, context: dict[str, Any]) -> dict[str, Any]:
    baseline = context["baseline"]
    if _sha256(payload) != baseline["artifact_sha256"]:
        raise ReceiptError("baseline artifact bytes differ from the signed context")
    try:
        artifact = _object(json.loads(payload), "baseline artifact")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError("baseline artifact is not JSON") from error
    expected_fields = {
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
        "legacy_controller_objects",
        "unauthorized_exception_objects",
        "inventory_sha256",
    }
    _exact_keys(artifact, expected_fields, "baseline artifact")
    if artifact["schema"] not in {
        "fs2-serve.nebius.ai/sai07-baseline-inventory/v3",
        "fs2-serve.nebius.ai/sai07-baseline-inventory/v4",
    }:
        raise ReceiptError("baseline artifact schema is unsupported")
    unsigned = dict(artifact)
    self_digest = unsigned.pop("inventory_sha256")
    if self_digest != _sha256(_canonical(unsigned)):
        raise ReceiptError("baseline artifact self-digest differs")
    inspected_namespaces = [
        "fs2-data",
        "fs2-models",
        "fs2-observability",
        "fs2-reference-data",
        "fs2-system",
        *context["scientific_namespaces"],
    ]
    expected_kinds = BASELINE_INVENTORY_KINDS if artifact["schema"].endswith("/v4") else LEGACY_BASELINE_INVENTORY_KINDS
    expected_collections = {
        (namespace, api_version, kind) for namespace in inspected_namespaces for api_version, kind in expected_kinds
    }
    observed_collections: dict[tuple[str, str, str], int] = {}
    collections = artifact["collections"]
    objects = artifact["objects"]
    if not isinstance(collections, list) or not isinstance(objects, list):
        raise ReceiptError("baseline artifact inventories must be lists")
    for index, collection_raw in enumerate(collections):
        collection = _object(collection_raw, f"baseline collection[{index}]")
        _exact_keys(
            collection,
            {"api_version", "kind", "namespace", "resource_version", "item_count"},
            f"baseline collection[{index}]",
        )
        key = (collection["namespace"], collection["api_version"], collection["kind"])
        if key in observed_collections or key not in expected_collections:
            raise ReceiptError("baseline collections are duplicated or outside the frozen inventory")
        _string(collection["resource_version"], f"baseline collection[{index}].resource_version")
        observed_collections[key] = _integer(collection["item_count"], f"baseline collection[{index}].item_count")
    if set(observed_collections) != expected_collections:
        raise ReceiptError("baseline artifact omits a frozen namespace/workload collection")
    observed_object_counts = {key: 0 for key in expected_collections}
    for index, item_raw in enumerate(objects):
        item = _object(item_raw, f"baseline object[{index}]")
        key = (item.get("namespace"), item.get("api_version"), item.get("kind"))
        if key not in observed_object_counts:
            raise ReceiptError("baseline object is outside the frozen inventory")
        observed_object_counts[key] += 1
    if observed_object_counts != observed_collections:
        raise ReceiptError("baseline object counts differ from collection snapshots")
    if (
        artifact["schema"] != baseline["schema"]
        or self_digest != baseline["inventory_sha256"]
        or artifact["reference_host_paths"] != baseline["reference_host_paths"]
        or artifact["baseline_incompatible_objects"] != baseline["baseline_incompatible_objects"]
        or artifact["restricted_incompatible_objects"] != baseline["restricted_incompatible_objects"]
        or artifact["scientific_namespaces"] != context["scientific_namespaces"]
        or artifact.get("cluster", {}).get("kube_system_uid") != context["kube_system_uid"]
        or artifact["inspected_namespaces"] != inspected_namespaces
        or not isinstance(artifact["legacy_controller_objects"], list)
        or artifact["unauthorized_exception_objects"] != []
    ):
        raise ReceiptError("baseline artifact inventory differs from the signed context")
    return artifact


def _validate_cleanup_result(
    payload: bytes,
    assertions: dict[str, Any],
    context: dict[str, Any],
    baseline_artifact: dict[str, Any],
) -> None:
    if _sha256(payload) != assertions["cleanup_result_sha256"]:
        raise ReceiptError("cleanup result bytes differ from the signed assertion")
    try:
        result = _object(json.loads(payload), "cleanup result")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError("cleanup result is not JSON") from error
    required = {
        "schema",
        "mode",
        "manifest_sha256",
        "baseline_artifact_sha256",
        "prior_inventory_sha256",
        "cluster_id",
        "run_id",
        "kube_system_uid",
        "fence_objects",
        "checked_objects",
        "removed_objects",
        "result_sha256",
    }
    _exact_keys(result, required, "cleanup result")
    unsigned = dict(result)
    result_self_digest = unsigned.pop("result_sha256")
    if (
        result["schema"] != "fs2-serve.nebius.ai/sai07-legacy-cleanup-result/v3"
        or result["mode"] != "execute"
        or result_self_digest != _sha256(_canonical(unsigned))
        or result["manifest_sha256"] != assertions["cleanup_manifest_sha256"]
        or result["baseline_artifact_sha256"] != context["baseline"]["artifact_sha256"]
        or result["prior_inventory_sha256"] != context["baseline"]["inventory_sha256"]
        or result["cluster_id"] != context["cluster_id"]
        or result["run_id"] != context["run_id"]
        or result["kube_system_uid"] != context["kube_system_uid"]
        or not isinstance(result["fence_objects"], list)
        or len(result["fence_objects"]) != 3
        or result["checked_objects"] != assertions["removed_objects"]
        or result["removed_objects"] != assertions["removed_objects"]
        or result["checked_objects"] != baseline_artifact["legacy_controller_objects"]
    ):
        raise ReceiptError("cleanup result does not bind the signed manifest, context, and removed objects")


def _resource_path(api_version: str, kind: str, namespace: str, name: str = "") -> str:
    identity = (api_version, kind)
    if identity not in RESOURCE_PATHS:
        raise ReceiptError(f"live observation kind {api_version}/{kind} is not allowed")
    resource = RESOURCE_PATHS[identity]
    if "/" in api_version:
        group, version = api_version.split("/", 1)
        prefix = f"/apis/{quote(group, safe='')}/{quote(version, safe='')}"
    else:
        prefix = f"/api/{quote(api_version, safe='')}"
    if identity in CLUSTER_SCOPED:
        if namespace:
            raise ReceiptError(f"cluster-scoped {kind} must not name a namespace")
        path = f"{prefix}/{resource}"
    else:
        if not DNS_RE.fullmatch(namespace):
            raise ReceiptError(f"namespaced {kind} has an invalid namespace")
        path = f"{prefix}/namespaces/{quote(namespace, safe='')}/{resource}"
    if name:
        path += f"/{quote(name, safe='')}"
    return path


def _metadata_projection(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace", ""),
        "uid": metadata.get("uid"),
        "resourceVersion": metadata.get("resourceVersion"),
        "generation": metadata.get("generation"),
        "labels": metadata.get("labels", {}),
        "annotations": metadata.get("annotations", {}),
        "ownerReferences": metadata.get("ownerReferences", []),
        "deletionTimestamp": metadata.get("deletionTimestamp"),
    }


def _live_projection(value: dict[str, Any]) -> dict[str, Any]:
    metadata = _object(value.get("metadata"), "live object metadata")
    projection: dict[str, Any] = {
        "apiVersion": value.get("apiVersion"),
        "kind": value.get("kind"),
        "metadata": _metadata_projection(metadata),
    }
    for key in (
        "spec",
        "status",
        "data",
        "template",
        "automountServiceAccountToken",
        "imagePullSecrets",
        "secrets",
        # StorageClass fields are top-level rather than nested under spec.
        "provisioner",
        "parameters",
        "reclaimPolicy",
        "volumeBindingMode",
        "allowVolumeExpansion",
        "mountOptions",
        "allowedTopologies",
    ):
        if key in value:
            projection[key] = value[key]
    return projection


def _object_key(value: dict[str, Any]) -> str:
    return "/".join(
        (
            value["api_version"],
            value["kind"],
            value["namespace"] or "_cluster",
            value["name"],
        )
    )


def _validate_object_observation(raw: Any, label: str) -> dict[str, Any]:
    value = _object(raw, label)
    _exact_keys(
        value,
        {
            "api_version",
            "kind",
            "namespace",
            "name",
            "exists",
            "uid",
            "resource_version",
            "object_sha256",
        },
        label,
    )
    api_version = _string(value["api_version"], f"{label}.api_version")
    kind = _string(value["kind"], f"{label}.kind")
    namespace = value["namespace"]
    if not isinstance(namespace, str):
        raise ReceiptError(f"{label}.namespace must be a string")
    name = _string(value["name"], f"{label}.name")
    if not DNS_RE.fullmatch(name):
        raise ReceiptError(f"{label}.name is malformed")
    _resource_path(api_version, kind, namespace, name)
    if value["exists"] is True:
        for field in ("uid", "resource_version"):
            if not IDENTIFIER_RE.fullmatch(_string(value[field], f"{label}.{field}")):
                raise ReceiptError(f"{label}.{field} is malformed")
        if not SHA256_RE.fullmatch(_string(value["object_sha256"], f"{label}.object_sha256")):
            raise ReceiptError(f"{label}.object_sha256 is malformed")
    elif value["exists"] is False:
        if any(value[field] is not None for field in ("uid", "resource_version", "object_sha256")):
            raise ReceiptError(f"{label} absent-object identity fields must be null")
    else:
        raise ReceiptError(f"{label}.exists must be a boolean")
    return value


def _validate_inventory(raw: Any, label: str) -> dict[str, Any]:
    value = _object(raw, label)
    _exact_keys(
        value,
        {
            "api_version",
            "kind",
            "namespace",
            "resource_version",
            "label_selector",
            "field_selector",
            "item_count",
            "items_sha256",
        },
        label,
    )
    api_version = _string(value["api_version"], f"{label}.api_version")
    kind = _string(value["kind"], f"{label}.kind")
    namespace = value["namespace"]
    if not isinstance(namespace, str):
        raise ReceiptError(f"{label}.namespace must be a string")
    _resource_path(api_version, kind, namespace)
    if not IDENTIFIER_RE.fullmatch(_string(value["resource_version"], f"{label}.resource_version")):
        raise ReceiptError(f"{label}.resource_version is malformed")
    for field in ("label_selector", "field_selector"):
        if not isinstance(value[field], str) or len(value[field]) > 1024:
            raise ReceiptError(f"{label}.{field} is malformed")
    _integer(value["item_count"], f"{label}.item_count")
    if not SHA256_RE.fullmatch(_string(value["items_sha256"], f"{label}.items_sha256")):
        raise ReceiptError(f"{label}.items_sha256 is malformed")
    return value


def _validate_observation_contract(
    state: str,
    observations: dict[str, Any],
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    _exact_keys(observations, {"observed_at", "objects", "inventories", "assertions"}, "observations")
    objects_raw = observations["objects"]
    inventories_raw = observations["inventories"]
    if not isinstance(objects_raw, list) or not 1 <= len(objects_raw) <= MAX_OBJECTS:
        raise ReceiptError("observations.objects must be a non-empty bounded list")
    if not isinstance(inventories_raw, list) or len(inventories_raw) > MAX_INVENTORIES:
        raise ReceiptError("observations.inventories must be a bounded list")
    objects = [
        _validate_object_observation(value, f"observations.objects[{index}]") for index, value in enumerate(objects_raw)
    ]
    inventories = [
        _validate_inventory(value, f"observations.inventories[{index}]") for index, value in enumerate(inventories_raw)
    ]
    object_keys = [_object_key(value) for value in objects]
    if object_keys != sorted(set(object_keys)):
        raise ReceiptError("observed objects must be sorted and unique")
    inventory_keys = [
        (
            value["api_version"],
            value["kind"],
            value["namespace"],
            value["label_selector"],
            value["field_selector"],
        )
        for value in inventories
    ]
    if inventory_keys != sorted(set(inventory_keys)):
        raise ReceiptError("observed inventories must be sorted and unique")

    assertions = _object(observations["assertions"], "observations.assertions")
    present = {
        (value["api_version"], value["kind"], value["namespace"], value["name"]) for value in objects if value["exists"]
    }
    absent = {
        (value["api_version"], value["kind"], value["namespace"], value["name"])
        for value in objects
        if not value["exists"]
    }
    if state == "baseline-captured":
        _exact_keys(
            assertions,
            {
                "baseline_artifact_sha256",
                "baseline_inventory_sha256",
                "baseline_reference_host_paths",
                "baseline_incompatible_objects",
                "baseline_restricted_incompatible_objects",
            },
            "baseline-captured assertions",
        )
        baseline = context["baseline"]
        if assertions != {
            "baseline_artifact_sha256": baseline["artifact_sha256"],
            "baseline_inventory_sha256": baseline["inventory_sha256"],
            "baseline_reference_host_paths": baseline["reference_host_paths"],
            "baseline_incompatible_objects": baseline["baseline_incompatible_objects"],
            "baseline_restricted_incompatible_objects": baseline["restricted_incompatible_objects"],
        }:
            raise ReceiptError("baseline-captured assertions differ from the signed artifact")
    elif state == "exception-ready":
        _exact_keys(assertions, {"legacy_agents_ready", "exception_agents_ready"}, "exception-ready assertions")
        if assertions != {"legacy_agents_ready": True, "exception_agents_ready": True}:
            raise ReceiptError("exception-ready assertions must both pass")
        required = {
            ("v1", "Namespace", "", "fs2-node-observability"),
            *{("apps/v1", "DaemonSet", "fs2-node-observability", name) for name in EXCEPTION_DAEMONSETS},
            *{("v1", "ServiceAccount", "fs2-node-observability", name) for name in EXCEPTION_SERVICE_ACCOUNTS},
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-node-observability-pods"),
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", "", "fs2-node-observability-pods"),
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-node-observability-daemonsets"),
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicyBinding",
                "",
                "fs2-node-observability-daemonsets",
            ),
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-node-observability-configs"),
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicyBinding",
                "",
                "fs2-node-observability-configs",
            ),
            ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", "", "fs2-pod-security-enforcement-fence"),
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicyBinding",
                "",
                "fs2-pod-security-enforcement-fence",
            ),
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicy",
                "",
                "fs2-pod-security-legacy-cleanup-fence",
            ),
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicyBinding",
                "",
                "fs2-pod-security-legacy-cleanup-fence",
            ),
            *{("v1", "ConfigMap", item["namespace"], item["name"]) for item in context["host_agent_configs"]},
            *SNAPSHOT_EXCEPTION_OBJECTS,
            *{
                ("apps/v1", "DaemonSet", identity[location]["namespace"], identity[location]["name"])
                for identity in context["host_agents"]
                for location in ("legacy", "exception")
            },
        }
        if not required.issubset(present):
            raise ReceiptError("exception-ready receipt omits a required live object")
    elif state == "reference-data-ready":
        _exact_keys(
            assertions,
            {
                "read_probe_passed",
                "source_tree_sha256",
                "target_tree_sha256",
                "successor_reference_claims_ready",
                "snapshot_checkpoint_durability_passed",
            },
            "reference-data-ready assertions",
        )
        tree = context["dataset"]["tree_sha256"]
        if assertions != {
            "read_probe_passed": True,
            "source_tree_sha256": tree,
            "target_tree_sha256": tree,
            "successor_reference_claims_ready": True,
            "snapshot_checkpoint_durability_passed": True,
        }:
            raise ReceiptError("reference-data-ready assertions do not bind the exact dataset")
        required = {
            ("v1", "PersistentVolumeClaim", "fs2-reference-data", "fs2-reference-data-rwx"),
            (
                "v1",
                "ConfigMap",
                "fs2-reference-data",
                context["storage_evidence"]["tools_config_map"],
            ),
            (
                "batch/v1",
                "Job",
                "fs2-reference-data",
                _probe_name("fs2-reference-data-read-probe", context),
            ),
            ("storage.k8s.io/v1", "StorageClass", "", "fs2-reference-data-retained-sc"),
            (
                "storage.k8s.io/v1",
                "StorageClass",
                "",
                SNAPSHOT_CHECKPOINT_STORAGE_CLASS,
            ),
            (
                "v1",
                "PersistentVolume",
                "",
                context["successor_storage"]["reference_source"]["persistent_volume_name"],
            ),
            *{
                ("v1", "PersistentVolume", "", name)
                for name in REFERENCE_SUCCESSOR_VOLUMES.values()
            },
            ("v1", "PersistentVolume", "", SNAPSHOT_CHECKPOINT_VOLUME),
            *{("v1", "PersistentVolumeClaim", namespace, name) for namespace, name in REFERENCE_SUCCESSOR_CLAIMS},
            *{
                ("v1", "ConfigMap", namespace, context["storage_evidence"]["tools_config_map"])
                for namespace, _name in REFERENCE_SUCCESSOR_CLAIMS
            },
            (
                "v1",
                "PersistentVolumeClaim",
                SNAPSHOT_CHECKPOINT_CLAIM[0],
                SNAPSHOT_CHECKPOINT_CLAIM[1],
            ),
            *{
                (
                    "batch/v1",
                    "Job",
                    SNAPSHOT_CHECKPOINT_CLAIM[0],
                    _checkpoint_probe_name(mode, context),
                )
                for mode in ("read", "write")
            },
            *{
                (
                    "batch/v1",
                    "Job",
                    namespace,
                    _probe_name(f"{name}-read-probe", context),
                )
                for namespace, name in REFERENCE_SUCCESSOR_CLAIMS
            },
        }
        if not required.issubset(present):
            raise ReceiptError(
                "reference-data-ready receipt omits canonical or successor PVC, probe, or retained StorageClass state"
            )
    elif state == "enforcement-quiesced":
        _exact_keys(
            assertions,
            {
                "workload_writes_fenced",
                "live_inventory_sha256",
                "live_reference_host_paths",
                "live_baseline_incompatible_objects",
                "live_restricted_incompatible_objects",
                "live_legacy_controller_objects",
            },
            "enforcement-quiesced assertions",
        )
        if assertions["workload_writes_fenced"] is not True:
            raise ReceiptError("enforcement quiesce fence is not asserted")
        if (
            any(
                assertions[field] != 0
                for field in (
                    "live_reference_host_paths",
                    "live_baseline_incompatible_objects",
                    "live_restricted_incompatible_objects",
                )
            )
            or assertions["live_legacy_controller_objects"] != []
        ):
            raise ReceiptError("enforcement-quiesced inventory is not clean")
        required = {
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicy",
                "",
                "fs2-pod-security-enforcement-fence",
            ),
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicyBinding",
                "",
                "fs2-pod-security-enforcement-fence",
            ),
        }
        if not required.issubset(present):
            raise ReceiptError("enforcement-quiesced receipt omits the admission fence")
    elif state == "cleanup-complete":
        _exact_keys(
            assertions,
            {
                "baseline_artifact_sha256",
                "baseline_inventory_sha256",
                "baseline_reference_host_paths",
                "baseline_incompatible_objects",
                "baseline_restricted_incompatible_objects",
                "cleanup_manifest_sha256",
                "cleanup_result_sha256",
                "removed_objects",
                "live_inventory_sha256",
                "live_reference_host_paths",
                "live_baseline_incompatible_objects",
                "live_restricted_incompatible_objects",
                "live_legacy_controller_objects",
            },
            "cleanup-complete assertions",
        )
        baseline = context["baseline"]
        if (
            assertions["baseline_artifact_sha256"] != baseline["artifact_sha256"]
            or assertions["baseline_inventory_sha256"] != baseline["inventory_sha256"]
            or assertions["baseline_reference_host_paths"] != baseline["reference_host_paths"]
            or assertions["baseline_incompatible_objects"] != baseline["baseline_incompatible_objects"]
            or assertions["baseline_restricted_incompatible_objects"] != baseline["restricted_incompatible_objects"]
        ):
            raise ReceiptError("cleanup-complete assertions do not bind the frozen baseline artifact")
        for field in (
            "cleanup_manifest_sha256",
            "cleanup_result_sha256",
            "live_inventory_sha256",
        ):
            if not SHA256_RE.fullmatch(_string(assertions[field], f"{field} digest")):
                raise ReceiptError(f"{field} is malformed")
        for field in (
            "live_reference_host_paths",
            "live_baseline_incompatible_objects",
            "live_restricted_incompatible_objects",
        ):
            if assertions[field] != 0:
                raise ReceiptError("cleanup-complete live inventory is not clean")
        if assertions["live_legacy_controller_objects"] != []:
            raise ReceiptError("cleanup-complete retains legacy controller-owned objects")
        removed = assertions["removed_objects"]
        if not isinstance(removed, list) or not all(isinstance(item, dict) for item in removed):
            raise ReceiptError("removed_objects must be a list of exact object identities")
        removed_keys: list[str] = []
        for index, item in enumerate(removed):
            _exact_keys(
                item,
                {"api_version", "kind", "namespace", "name", "uid", "resource_version", "object_sha256"},
                f"removed_objects[{index}]",
            )
            for field in ("api_version", "kind", "namespace", "name", "uid", "resource_version"):
                _string(item[field], f"removed_objects[{index}].{field}")
            if not SHA256_RE.fullmatch(item["object_sha256"]):
                raise ReceiptError("removed object hash is malformed")
            removed_keys.append("/".join((item["api_version"], item["kind"], item["namespace"], item["name"])))
        if removed_keys != sorted(set(removed_keys)):
            raise ReceiptError("removed_objects must be sorted and unique")
        absent_keys = {"/".join((item[0], item[1], item[2], item[3])) for item in absent}
        if set(removed_keys) != absent_keys:
            raise ReceiptError("cleanup-complete absent objects differ from the signed cleanup result")
    elif state == "baseline-enforced":
        _exact_keys(assertions, {"privileged_probe_rejected", "positive_smoke_passed"}, "baseline-enforced assertions")
        if not all(assertions.values()):
            raise ReceiptError("baseline enforcement probes did not pass")
    elif state in {"enforcement-removed", "host-agents-restored"}:
        _exact_keys(assertions, {"host_agents_ready"}, f"{state} assertions")
        if assertions["host_agents_ready"] is not True:
            raise ReceiptError(f"{state} host-agent assertion did not pass")
        required_namespaces = {
            ("v1", "Namespace", "", name)
            for name in {
                "fs2-data",
                "fs2-models",
                "fs2-observability",
                "fs2-reference-data",
                "fs2-system",
                *context["scientific_namespaces"],
            }
        }
        if not required_namespaces.issubset(present):
            raise ReceiptError(f"{state} omits the exact baseline namespace inventory")
        if state == "host-agents-restored":
            required = {
                ("apps/v1", "DaemonSet", identity["legacy"]["namespace"], identity["legacy"]["name"])
                for identity in context["host_agents"]
            }
            if not required.issubset(present):
                raise ReceiptError("host-agents-restored omits a legacy host-agent DaemonSet")
    else:
        raise ReceiptError(f"unsupported observed state {state}")

    if state in {"baseline-captured", "cleanup-complete", "enforcement-quiesced", "baseline-enforced"}:
        target_namespaces = {
            "fs2-data",
            "fs2-models",
            "fs2-observability",
            "fs2-reference-data",
            "fs2-system",
            *context["scientific_namespaces"],
        }
        namespace_objects = {
            value["name"]
            for value in objects
            if value["exists"] and value["api_version"] == "v1" and value["kind"] == "Namespace"
        }
        if namespace_objects != target_namespaces:
            raise ReceiptError("baseline receipt does not cover the exact frozen namespace inventory")
        coverage = {
            (value["namespace"], value["api_version"], value["kind"])
            for value in inventories
            if not value["label_selector"] and not value["field_selector"]
        }
        required_coverage = {
            (namespace, api_version, kind)
            for namespace in target_namespaces
            for api_version, kind in BASELINE_INVENTORY_KINDS
        }
        if not required_coverage.issubset(coverage):
            raise ReceiptError("baseline receipt omits a complete live workload inventory")
    return objects, inventories, assertions


def _pod_templates(value: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return every Pod spec and its template annotations from a live object."""

    kind = value.get("kind")
    metadata = value.get("metadata", {})
    if kind == "PodTemplate":
        template = value.get("template", {})
        if isinstance(template, dict) and isinstance(template.get("spec"), dict):
            template_metadata = template.get("metadata", {})
            return [
                (
                    template["spec"],
                    template_metadata.get("annotations", {}) if isinstance(template_metadata, dict) else {},
                )
            ]
        return []
    spec = value.get("spec", {})
    if not isinstance(spec, dict):
        return []
    if kind == "Pod":
        return [(spec, metadata.get("annotations", {}) if isinstance(metadata, dict) else {})]
    template: Any = None
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job"}:
        template = spec.get("template")
    elif kind == "CronJob":
        template = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")
    if isinstance(template, dict) and isinstance(template.get("spec"), dict):
        template_metadata = template.get("metadata", {})
        return [
            (template["spec"], template_metadata.get("annotations", {}) if isinstance(template_metadata, dict) else {})
        ]
    if kind == "JobSet":
        results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for replicated_job in spec.get("replicatedJobs", []) or []:
            if not isinstance(replicated_job, dict):
                continue
            job_template = replicated_job.get("template", {})
            pod_template = (job_template.get("spec", {}) if isinstance(job_template, dict) else {}).get("template", {})
            if isinstance(pod_template, dict) and isinstance(pod_template.get("spec"), dict):
                template_metadata = pod_template.get("metadata", {})
                results.append(
                    (
                        pod_template["spec"],
                        template_metadata.get("annotations", {}) if isinstance(template_metadata, dict) else {},
                    )
                )
        return results
    return []


def _pod_security_findings(spec: dict[str, Any], annotations: dict[str, Any]) -> tuple[bool, bool, int]:
    baseline: set[str] = set()
    restricted: set[str] = set()
    host_paths = 0
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(field) is True:
            baseline.add(field)
    for volume in spec.get("volumes", []) or []:
        if isinstance(volume, dict) and "hostPath" in volume:
            baseline.add("hostPath")
            host_paths = 1
    pod_security = spec.get("securityContext", {}) or {}
    if not isinstance(pod_security, dict):
        pod_security = {}
    if pod_security.get("runAsUser") == 0 or pod_security.get("runAsNonRoot") is False:
        restricted.add("root")
    if pod_security.get("runAsNonRoot") is not True:
        restricted.add("runAsNonRoot")
    pod_seccomp = pod_security.get("seccompProfile", {}) or {}
    if not pod_seccomp:
        restricted.add("seccompProfile")
    elif isinstance(pod_seccomp, dict) and pod_seccomp.get("type") == "Unconfined":
        baseline.add("unconfinedSeccomp")
        restricted.add("unconfinedSeccomp")
    containers = [
        *(spec.get("initContainers", []) or []),
        *(spec.get("containers", []) or []),
        *(spec.get("ephemeralContainers", []) or []),
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        security = container.get("securityContext", {}) or {}
        if not isinstance(security, dict):
            security = {}
        if security.get("privileged") is True:
            baseline.add("privileged")
            restricted.add("privileged")
        if security.get("runAsUser") == 0 or security.get("runAsNonRoot") is False:
            restricted.add("root")
        if security.get("runAsNonRoot") is not True and pod_security.get("runAsNonRoot") is not True:
            restricted.add("runAsNonRoot")
        if security.get("allowPrivilegeEscalation") is not False:
            restricted.add("allowPrivilegeEscalation")
        if security.get("procMount") not in (None, "Default"):
            baseline.add("procMount")
        seccomp = security.get("seccompProfile", {}) or {}
        if isinstance(seccomp, dict) and seccomp.get("type") == "Unconfined":
            baseline.add("unconfinedSeccomp")
            restricted.add("unconfinedSeccomp")
        if not seccomp and not pod_seccomp:
            restricted.add("seccompProfile")
        apparmor = security.get("appArmorProfile", {}) or {}
        if isinstance(apparmor, dict) and apparmor.get("type") == "Unconfined":
            baseline.add("unconfinedAppArmor")
            restricted.add("unconfinedAppArmor")
        capabilities = security.get("capabilities", {}) or {}
        added = set(capabilities.get("add", []) or []) if isinstance(capabilities, dict) else set()
        dropped = set(capabilities.get("drop", []) or []) if isinstance(capabilities, dict) else set()
        if not added.issubset(BASELINE_CAPABILITIES):
            baseline.add("capabilities")
        if added or "ALL" not in dropped:
            restricted.add("capabilities")
        for port in container.get("ports", []) or []:
            if isinstance(port, dict) and int(port.get("hostPort", 0) or 0):
                baseline.add("hostPort")
    if any(
        key.startswith("container.apparmor.security.beta.kubernetes.io/") and value == "unconfined"
        for key, value in annotations.items()
    ):
        baseline.add("unconfinedAppArmor")
        restricted.add("unconfinedAppArmor")
    return bool(baseline), bool(restricted), host_paths


def _job_completed_once(value: dict[str, Any], label: str) -> dict[str, Any]:
    status = _object(value.get("status"), f"{label} status")
    if int(status.get("succeeded", 0) or 0) != 1 or not any(
        isinstance(condition, dict) and condition.get("type") == "Complete" and condition.get("status") == "True"
        for condition in status.get("conditions", []) or []
    ):
        raise ReceiptError(f"{label} is not exactly completed")
    spec = _object(value.get("spec"), f"{label} spec")
    template = _object(spec.get("template"), f"{label} template")
    return _object(template.get("spec"), f"{label} Pod spec")


def _probe_name(prefix: str, context: dict[str, Any]) -> str:
    tree = context["dataset"]["tree_sha256"]
    challenge_sha256 = hashlib.sha256(context["deployment_nonce"].encode()).hexdigest()
    return f"{prefix}-{tree[:12]}-{challenge_sha256[:12]}"


def _checkpoint_probe_name(mode: str, context: dict[str, Any]) -> str:
    challenge_sha256 = hashlib.sha256(context["deployment_nonce"].encode()).hexdigest()
    return f"fs2-snapshot-checkpoints-durability-{mode}-{challenge_sha256[:12]}"


def _validate_storage_tooling(
    live_objects: dict[tuple[str, str, str, str], dict[str, Any]],
    namespace: str,
    context: dict[str, Any],
) -> None:
    name = context["storage_evidence"]["tools_config_map"]
    config = live_objects.get(("v1", "ConfigMap", namespace, name))
    if config is None or config.get("immutable") is not True:
        raise ReceiptError(f"immutable storage proof tooling is absent from {namespace}")
    data = _object(config.get("data"), f"storage proof tooling in {namespace}")
    if _sha256(_canonical(data)) != context["storage_evidence"]["tools_data_sha256"]:
        raise ReceiptError(f"storage proof tooling content differs in {namespace}")


def _validate_read_probe_execution(
    live_objects: dict[tuple[str, str, str, str], dict[str, Any]],
    claim: dict[str, Any],
    job: dict[str, Any],
    context: dict[str, Any],
    *,
    label: str,
    claim_name: str,
) -> None:
    job_metadata = _object(job.get("metadata"), f"{label} Job metadata")
    job_namespace = _string(job_metadata.get("namespace"), f"{label} Job namespace")
    _validate_storage_tooling(live_objects, job_namespace, context)
    claim_metadata = _object(claim.get("metadata"), f"{label} claim metadata")
    claim_spec = _object(claim.get("spec"), f"{label} claim spec")
    claim_identity = {
        "uid": _string(claim_metadata.get("uid"), f"{label} claim UID"),
        "resource_version": _string(
            claim_metadata.get("resourceVersion"), f"{label} claim resourceVersion"
        ),
        "volume_name": _string(claim_spec.get("volumeName"), f"{label} claim volumeName"),
    }
    job_uid = _string(job_metadata.get("uid"), f"{label} Job UID")
    job_annotations = _object(job_metadata.get("annotations", {}), f"{label} Job annotations")
    expected_annotations = {
        "reference-data.fs2.nebius.ai/tree-sha256": context["dataset"]["tree_sha256"],
        "reference-data.fs2.nebius.ai/receipt": (
            f"receipts/{context['dataset']['id']}/{context['dataset']['revision']}.json"
        ),
        "reference-data.fs2.nebius.ai/pvc-uid": claim_identity["uid"],
        "reference-data.fs2.nebius.ai/pvc-resource-version": claim_identity["resource_version"],
        "reference-data.fs2.nebius.ai/volume-name": claim_identity["volume_name"],
        "reference-data.fs2.nebius.ai/proof-challenge": context["deployment_nonce"],
    }
    if any(job_annotations.get(key) != value for key, value in expected_annotations.items()):
        raise ReceiptError(f"{label} Job does not bind the signed PVC, dataset, and challenge")

    pod_spec = _job_completed_once(job, label)
    containers = pod_spec.get("containers")
    volumes = pod_spec.get("volumes")
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(volumes, list):
        raise ReceiptError(f"{label} must have exactly one container and a bounded volume list")
    container = _object(containers[0], f"{label} container")
    expected_command = [
        "python",
        "/opt/fs2/reference-data/verify_csi_readiness.py",
        "--root",
        "/reference-data",
        "--receipt",
        expected_annotations["reference-data.fs2.nebius.ai/receipt"],
        "--bundle",
        context["dataset"]["id"],
        "--revision",
        context["dataset"]["revision"],
        "--tree-sha256",
        context["dataset"]["tree_sha256"],
        "--pvc-uid",
        claim_identity["uid"],
        "--pvc-resource-version",
        claim_identity["resource_version"],
        "--volume-name",
        claim_identity["volume_name"],
        "--challenge",
        context["deployment_nonce"],
        "--proof-output",
        "/dev/termination-log",
    ]
    if (
        pod_spec.get("automountServiceAccountToken") is not False
        or container.get("name") != "read-probe"
        or container.get("image") != context["storage_evidence"]["probe_image"]
        or container.get("command") != expected_command
        or container.get("terminationMessagePath") != "/dev/termination-log"
        or container.get("terminationMessagePolicy") != "File"
        or not any(
            isinstance(volume, dict)
            and isinstance(volume.get("persistentVolumeClaim"), dict)
            and volume["persistentVolumeClaim"].get("claimName") == claim_name
            and volume["persistentVolumeClaim"].get("readOnly") is True
            for volume in volumes
        )
        or not any(
            isinstance(volume, dict)
            and volume.get("name") == "tools"
            and isinstance(volume.get("configMap"), dict)
            and volume["configMap"].get("name") == context["storage_evidence"]["tools_config_map"]
            for volume in volumes
        )
    ):
        raise ReceiptError(f"{label} execution contract is not exact, tokenless, and read-only")

    owned_pods = []
    for (api_version, kind, namespace, _name), pod in live_objects.items():
        if api_version != "v1" or kind != "Pod" or namespace != job_metadata.get("namespace"):
            continue
        metadata = _object(pod.get("metadata"), f"{label} Pod metadata")
        owner_references = metadata.get("ownerReferences", [])
        if isinstance(owner_references, list) and any(
            isinstance(owner, dict)
            and owner.get("apiVersion") == "batch/v1"
            and owner.get("kind") == "Job"
            and owner.get("uid") == job_uid
            and owner.get("controller") is True
            for owner in owner_references
        ):
            owned_pods.append(pod)
    if len(owned_pods) != 1:
        raise ReceiptError(f"{label} must have exactly one live Pod owned by the exact Job UID")
    pod = owned_pods[0]
    live_pod_spec = _object(pod.get("spec"), f"{label} live Pod spec")
    live_containers = live_pod_spec.get("containers")
    statuses = _object(pod.get("status"), f"{label} live Pod status").get("containerStatuses")
    if (
        not isinstance(live_containers, list)
        or len(live_containers) != 1
        or not isinstance(statuses, list)
        or len(statuses) != 1
    ):
        raise ReceiptError(f"{label} live Pod execution is ambiguous")
    live_container = _object(live_containers[0], f"{label} live container")
    status = _object(statuses[0], f"{label} live container status")
    terminated = _object(
        _object(status.get("state"), f"{label} container state").get("terminated"),
        f"{label} termination",
    )
    image_digest = context["storage_evidence"]["probe_image"].split("@", 1)[1]
    if (
        live_container.get("image") != context["storage_evidence"]["probe_image"]
        or live_container.get("command") != expected_command
        or not _string(status.get("imageID"), f"{label} runtime image ID").endswith(image_digest)
        or terminated.get("exitCode") != 0
    ):
        raise ReceiptError(f"{label} live Pod did not run the exact digest-pinned proof command")
    try:
        proof = _object(json.loads(_string(terminated.get("message"), f"{label} termination proof")), f"{label} proof")
    except json.JSONDecodeError as error:
        raise ReceiptError(f"{label} termination proof is not canonical JSON") from error
    _exact_keys(
        proof,
        {
            "schema",
            "bundle_id",
            "revision",
            "tree_sha256",
            "receipt_sha256",
            "manifest_sha256",
            "read_probe_passed",
            "pvc",
            "challenge",
            "proof_sha256",
        },
        f"{label} proof",
    )
    proof_sha256 = proof.pop("proof_sha256")
    if (
        proof.get("schema") != context["storage_evidence"]["read_proof_schema"]
        or proof.get("bundle_id") != context["dataset"]["id"]
        or proof.get("revision") != context["dataset"]["revision"]
        or proof.get("tree_sha256") != context["dataset"]["tree_sha256"]
        or proof.get("read_probe_passed") is not True
        or proof.get("pvc") != claim_identity
        or proof.get("challenge") != context["deployment_nonce"]
        or not SHA256_RE.fullmatch(str(proof.get("receipt_sha256", "")))
        or not SHA256_RE.fullmatch(str(proof.get("manifest_sha256", "")))
        or proof_sha256 != _sha256(_canonical(proof))
    ):
        raise ReceiptError(f"{label} termination proof does not bind the signed live execution")


def _validate_checkpoint_probe_execution(
    live_objects: dict[tuple[str, str, str, str], dict[str, Any]],
    claim: dict[str, Any],
    job: dict[str, Any],
    context: dict[str, Any],
    *,
    mode: str,
) -> dt.datetime:
    label = f"snapshot checkpoint {mode} proof"
    namespace = SNAPSHOT_CHECKPOINT_CLAIM[0]
    _validate_storage_tooling(live_objects, namespace, context)
    claim_metadata = _object(claim.get("metadata"), f"{label} claim metadata")
    claim_spec = _object(claim.get("spec"), f"{label} claim spec")
    claim_identity = {
        "uid": _string(claim_metadata.get("uid"), f"{label} PVC UID"),
        "resource_version": _string(claim_metadata.get("resourceVersion"), f"{label} PVC resourceVersion"),
        "volume_name": _string(claim_spec.get("volumeName"), f"{label} PVC volumeName"),
    }
    annotations = {
        "security.fs2.nebius.ai/pvc-uid": claim_identity["uid"],
        "security.fs2.nebius.ai/pvc-resource-version": claim_identity["resource_version"],
        "security.fs2.nebius.ai/volume-name": claim_identity["volume_name"],
        "security.fs2.nebius.ai/proof-challenge": context["deployment_nonce"],
        "security.fs2.nebius.ai/proof-mode": mode,
    }
    job_metadata = _object(job.get("metadata"), f"{label} Job metadata")
    job_annotations = _object(job_metadata.get("annotations", {}), f"{label} Job annotations")
    if any(job_annotations.get(key) != value for key, value in annotations.items()):
        raise ReceiptError(f"{label} Job does not bind the live PVC and signed challenge")
    command = [
        "python",
        "/opt/fs2/reference-data/verify_checkpoint_durability.py",
        mode,
        "--root",
        "/checkpoints",
        "--pvc-uid",
        claim_identity["uid"],
        "--pvc-resource-version",
        claim_identity["resource_version"],
        "--volume-name",
        claim_identity["volume_name"],
        "--challenge",
        context["deployment_nonce"],
        "--proof-output",
        "/dev/termination-log",
    ]
    pod_spec = _job_completed_once(job, label)
    containers = pod_spec.get("containers")
    volumes = pod_spec.get("volumes")
    expected_read_only = mode == "read"
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(volumes, list):
        raise ReceiptError(f"{label} execution shape is ambiguous")
    container = _object(containers[0], f"{label} container")
    if (
        pod_spec.get("automountServiceAccountToken") is not False
        or container.get("name") != "durability-proof"
        or container.get("image") != context["storage_evidence"]["probe_image"]
        or container.get("command") != command
        or container.get("terminationMessagePath") != "/dev/termination-log"
        or container.get("terminationMessagePolicy") != "File"
        or not any(
            isinstance(volume, dict)
            and volume.get("name") == "checkpoints"
            and isinstance(volume.get("persistentVolumeClaim"), dict)
            and volume["persistentVolumeClaim"].get("claimName") == SNAPSHOT_CHECKPOINT_CLAIM[1]
            and volume["persistentVolumeClaim"].get("readOnly", False) is expected_read_only
            for volume in volumes
        )
        or not any(
            isinstance(volume, dict)
            and volume.get("name") == "tools"
            and isinstance(volume.get("configMap"), dict)
            and volume["configMap"].get("name") == context["storage_evidence"]["tools_config_map"]
            for volume in volumes
        )
    ):
        raise ReceiptError(f"{label} execution contract differs")

    job_uid = _string(job_metadata.get("uid"), f"{label} Job UID")
    owned_pods = []
    for (api_version, kind, pod_namespace, _name), pod in live_objects.items():
        if api_version != "v1" or kind != "Pod" or pod_namespace != namespace:
            continue
        metadata = _object(pod.get("metadata"), f"{label} Pod metadata")
        owners = metadata.get("ownerReferences", [])
        if isinstance(owners, list) and any(
            isinstance(owner, dict)
            and owner.get("apiVersion") == "batch/v1"
            and owner.get("kind") == "Job"
            and owner.get("uid") == job_uid
            and owner.get("controller") is True
            for owner in owners
        ):
            owned_pods.append(pod)
    if len(owned_pods) != 1:
        raise ReceiptError(f"{label} must have exactly one Pod owned by the exact Job UID")
    pod = owned_pods[0]
    live_containers = _object(pod.get("spec"), f"{label} live Pod spec").get("containers")
    statuses = _object(pod.get("status"), f"{label} live Pod status").get("containerStatuses")
    if (
        not isinstance(live_containers, list)
        or len(live_containers) != 1
        or not isinstance(statuses, list)
        or len(statuses) != 1
    ):
        raise ReceiptError(f"{label} live Pod execution is ambiguous")
    if _object(live_containers[0], f"{label} live container").get("command") != command:
        raise ReceiptError(f"{label} live Pod command differs")
    status = _object(statuses[0], f"{label} container status")
    terminated = _object(
        _object(status.get("state"), f"{label} state").get("terminated"),
        f"{label} termination",
    )
    image_digest = context["storage_evidence"]["probe_image"].split("@", 1)[1]
    if (
        not _string(status.get("imageID"), f"{label} image ID").endswith(image_digest)
        or terminated.get("exitCode") != 0
    ):
        raise ReceiptError(f"{label} did not use the exact runtime image successfully")
    try:
        proof = _object(json.loads(_string(terminated.get("message"), f"{label} output")), f"{label} output")
    except json.JSONDecodeError as error:
        raise ReceiptError(f"{label} output is not canonical JSON") from error
    _exact_keys(
        proof,
        {"schema", "mode", "pvc", "challenge", "marker_sha256", "proof_sha256"},
        f"{label} output",
    )
    proof_sha256 = proof.pop("proof_sha256")
    marker = {
        "schema": "fs2-serve.nebius.ai/checkpoint-durability-marker/v1",
        "pvc": claim_identity,
        "challenge": context["deployment_nonce"],
    }
    if (
        proof.get("schema") != context["storage_evidence"]["checkpoint_proof_schema"]
        or proof.get("mode") != mode
        or proof.get("pvc") != claim_identity
        or proof.get("challenge") != context["deployment_nonce"]
        or proof.get("marker_sha256") != _sha256(_canonical(marker) + b"\n")
        or proof_sha256 != _sha256(_canonical(proof))
    ):
        raise ReceiptError(f"{label} output does not bind the exact remounted content")
    return _instant(terminated.get("finishedAt"), f"{label} finishedAt")


def _validate_bound_retained_claim(
    value: dict[str, Any],
    *,
    storage_class: str,
    allowed_access_modes: set[tuple[str, ...]],
    label: str,
) -> None:
    spec = _object(value.get("spec"), f"{label} spec")
    status = _object(value.get("status"), f"{label} status")
    access_modes = spec.get("accessModes")
    if (
        not isinstance(access_modes, list)
        or tuple(access_modes) not in allowed_access_modes
        or spec.get("storageClassName") != storage_class
        or status.get("phase") != "Bound"
        or not isinstance(_object(spec.get("resources"), f"{label} resources").get("requests"), dict)
        or not _object(spec["resources"].get("requests"), f"{label} requests").get("storage")
    ):
        raise ReceiptError(f"{label} is not the exact Bound retained claim")


def _validate_storage_successors(
    live_objects: dict[tuple[str, str, str, str], dict[str, Any]],
    context: dict[str, Any],
) -> None:
    tree = context["dataset"]["tree_sha256"]
    custody = _object(context["successor_storage"], "successor storage custody")
    reference_source = _object(custody["reference_source"], "reference source custody")
    checkpoint_source = _object(custody["checkpoint_source"], "checkpoint source custody")
    reference_class = live_objects.get(("storage.k8s.io/v1", "StorageClass", "", REFERENCE_SUCCESSOR_STORAGE_CLASS))
    checkpoint_class = live_objects.get(("storage.k8s.io/v1", "StorageClass", "", SNAPSHOT_CHECKPOINT_STORAGE_CLASS))
    if (
        reference_class is None
        or checkpoint_class is None
        or reference_class.get("reclaimPolicy") != "Retain"
        or checkpoint_class.get("reclaimPolicy") != "Retain"
    ):
        raise ReceiptError("successor storage classes are absent or do not retain volumes")

    source_volume = live_objects.get(
        ("v1", "PersistentVolume", "", reference_source["persistent_volume_name"])
    )
    if source_volume is None:
        raise ReceiptError("canonical reference source PV is absent")
    source_metadata = _object(source_volume.get("metadata"), "canonical reference source PV metadata")
    source_spec = _object(source_volume.get("spec"), "canonical reference source PV spec")
    source_csi = _object(source_spec.get("csi"), "canonical reference source PV CSI source")
    if (
        context["pvc"]["volume_name"] != reference_source["persistent_volume_name"]
        or source_metadata.get("name") != context["pvc"]["volume_name"]
        or source_metadata.get("uid") != reference_source["uid"]
        or source_metadata.get("resourceVersion") != reference_source["resource_version"]
        or source_spec.get("storageClassName") != REFERENCE_SUCCESSOR_STORAGE_CLASS
        or source_spec.get("persistentVolumeReclaimPolicy") != "Retain"
        or _object(source_spec.get("capacity"), "canonical reference source PV capacity").get("storage")
        != reference_source["capacity_quantity"]
        or source_csi.get("driver") != reference_source["csi_driver"]
        or source_csi.get("volumeHandle") != reference_source["volume_handle"]
        or source_csi.get("volumeAttributes", {}) != reference_source["volume_attributes"]
    ):
        raise ReceiptError("canonical reference source PV differs from signed custody")

    for namespace, name in REFERENCE_SUCCESSOR_CLAIMS:
        label = f"reference successor {namespace}/{name}"
        volume_name = REFERENCE_SUCCESSOR_VOLUMES[(namespace, name)]
        volume = live_objects.get(("v1", "PersistentVolume", "", volume_name))
        claim = live_objects.get(("v1", "PersistentVolumeClaim", namespace, name))
        probe_name = _probe_name(f"{name}-read-probe", context)
        probe = live_objects.get(("batch/v1", "Job", namespace, probe_name))
        if volume is None or claim is None or probe is None:
            raise ReceiptError(f"{label} retained PV, claim, or content probe is absent")
        volume_metadata = _object(volume.get("metadata"), f"{label} PV metadata")
        volume_annotations = _object(
            volume_metadata.get("annotations", {}), f"{label} PV annotations"
        )
        volume_spec = _object(volume.get("spec"), f"{label} PV spec")
        volume_csi = _object(volume_spec.get("csi"), f"{label} PV CSI source")
        volume_claim_ref = _object(volume_spec.get("claimRef"), f"{label} PV claim reference")
        if (
            volume_spec.get("accessModes") != ["ReadOnlyMany"]
            or volume_spec.get("persistentVolumeReclaimPolicy") != "Retain"
            or volume_spec.get("storageClassName") != REFERENCE_SUCCESSOR_STORAGE_CLASS
            or _object(volume_spec.get("capacity"), f"{label} PV capacity").get("storage")
            != f"{reference_source['capacity_gib']}Gi"
            or any(
                volume_claim_ref.get(field) != value
                for field, value in {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "name": name,
                    "namespace": namespace,
                }.items()
            )
            or volume_csi.get("driver") != reference_source["csi_driver"]
            or volume_csi.get("volumeHandle") != reference_source["volume_handle"]
            or volume_csi.get("volumeAttributes", {}) != reference_source["volume_attributes"]
            or volume_csi.get("readOnly") is not True
            or volume_annotations.get("security.fs2.nebius.ai/custody-contract-sha256")
            != context["successor_storage_sha256"]
            or volume_annotations.get("security.fs2.nebius.ai/provisioning-receipt-sha256")
            != reference_source["provisioning_receipt_sha256"]
            or volume_annotations.get("security.fs2.nebius.ai/storage-owner")
            != reference_source["storage_owner"]
        ):
            raise ReceiptError(f"{label} PV does not alias the exact retained source read-only")
        _validate_bound_retained_claim(
            claim,
            storage_class=REFERENCE_SUCCESSOR_STORAGE_CLASS,
            allowed_access_modes={("ReadOnlyMany",)},
            label=label,
        )
        claim_annotations = _object(
            _object(claim.get("metadata"), f"{label} metadata").get("annotations", {}),
            f"{label} annotations",
        )
        if (
            claim_annotations.get("security.fs2.nebius.ai/content-tree-sha256") != tree
            or claim_annotations.get("security.fs2.nebius.ai/custody-contract-sha256")
            != context["successor_storage_sha256"]
        ):
            raise ReceiptError(f"{label} does not bind the exact dataset tree and custody")
        if _object(claim.get("spec"), f"{label} claim spec").get("volumeName") != volume_name:
            raise ReceiptError(f"{label} claim is not bound to its fixed retained PV")
        pod_spec = _job_completed_once(probe, f"{label} read probe")
        volumes = pod_spec.get("volumes")
        if (
            pod_spec.get("automountServiceAccountToken") is not False
            or not isinstance(volumes, list)
            or not any(
                isinstance(volume, dict)
                and isinstance(volume.get("persistentVolumeClaim"), dict)
                and volume["persistentVolumeClaim"].get("claimName") == name
                and volume["persistentVolumeClaim"].get("readOnly") is True
                for volume in volumes
            )
        ):
            raise ReceiptError(f"{label} probe is not tokenless and read-only")
        probe_annotations = _object(
            _object(probe.get("metadata"), f"{label} probe metadata").get("annotations", {}),
            f"{label} probe annotations",
        )
        if probe_annotations.get("security.fs2.nebius.ai/verified-tree-sha256") != tree:
            raise ReceiptError(f"{label} probe does not attest the exact dataset tree")
        _validate_read_probe_execution(
            live_objects,
            claim,
            probe,
            context,
            label=f"{label} read probe",
            claim_name=name,
        )

    checkpoint_namespace, checkpoint_name = SNAPSHOT_CHECKPOINT_CLAIM
    checkpoint_volume = live_objects.get(("v1", "PersistentVolume", "", SNAPSHOT_CHECKPOINT_VOLUME))
    checkpoint = live_objects.get(("v1", "PersistentVolumeClaim", checkpoint_namespace, checkpoint_name))
    if checkpoint_volume is None or checkpoint is None:
        raise ReceiptError("snapshot checkpoint successor PV or claim is absent")
    checkpoint_volume_annotations = _object(
        _object(checkpoint_volume.get("metadata"), "snapshot checkpoint PV metadata").get(
            "annotations", {}
        ),
        "snapshot checkpoint PV annotations",
    )
    checkpoint_volume_spec = _object(checkpoint_volume.get("spec"), "snapshot checkpoint PV spec")
    checkpoint_csi = _object(checkpoint_volume_spec.get("csi"), "snapshot checkpoint PV CSI source")
    checkpoint_claim_ref = _object(checkpoint_volume_spec.get("claimRef"), "snapshot checkpoint PV claim reference")
    if (
        checkpoint_volume_spec.get("accessModes") != ["ReadWriteMany"]
        or checkpoint_volume_spec.get("persistentVolumeReclaimPolicy") != "Retain"
        or checkpoint_volume_spec.get("storageClassName") != SNAPSHOT_CHECKPOINT_STORAGE_CLASS
        or _object(checkpoint_volume_spec.get("capacity"), "snapshot checkpoint PV capacity").get("storage")
        != f"{checkpoint_source['capacity_gib']}Gi"
        or any(
            checkpoint_claim_ref.get(field) != value
            for field, value in {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "name": checkpoint_name,
                "namespace": checkpoint_namespace,
            }.items()
        )
        or checkpoint_csi.get("driver") != checkpoint_source["csi_driver"]
        or checkpoint_csi.get("volumeHandle") != checkpoint_source["volume_handle"]
        or checkpoint_csi.get("volumeAttributes", {}) != checkpoint_source["volume_attributes"]
        or checkpoint_csi.get("readOnly", False) is not False
        or checkpoint_volume_annotations.get("security.fs2.nebius.ai/custody-contract-sha256")
        != context["successor_storage_sha256"]
        or checkpoint_volume_annotations.get("security.fs2.nebius.ai/provisioning-receipt-sha256")
        != checkpoint_source["provisioning_receipt_sha256"]
        or checkpoint_volume_annotations.get("security.fs2.nebius.ai/storage-owner")
        != checkpoint_source["storage_owner"]
    ):
        raise ReceiptError("snapshot checkpoint PV differs from signed distinct custody")
    _validate_bound_retained_claim(
        checkpoint,
        storage_class=SNAPSHOT_CHECKPOINT_STORAGE_CLASS,
        allowed_access_modes={("ReadWriteMany",)},
        label="snapshot checkpoint successor",
    )
    checkpoint_spec = _object(checkpoint.get("spec"), "snapshot checkpoint claim spec")
    checkpoint_annotations = _object(
        _object(checkpoint.get("metadata"), "snapshot checkpoint claim metadata").get(
            "annotations", {}
        ),
        "snapshot checkpoint claim annotations",
    )
    if (
        checkpoint_spec.get("volumeName") != SNAPSHOT_CHECKPOINT_VOLUME
        or _object(checkpoint_spec.get("resources"), "snapshot checkpoint resources")
        .get("requests", {})
        .get("storage")
        != f"{checkpoint_source['requested_gib']}Gi"
        or checkpoint_annotations.get("security.fs2.nebius.ai/custody-contract-sha256")
        != context["successor_storage_sha256"]
    ):
        raise ReceiptError("snapshot checkpoint claim is not bound to the exact retained PV and capacity")
    jobs = {
        mode: live_objects.get(
            ("batch/v1", "Job", checkpoint_namespace, _checkpoint_probe_name(mode, context))
        )
        for mode in ("write", "read")
    }
    if any(job is None for job in jobs.values()):
        raise ReceiptError("snapshot checkpoint write/remount-read proof is incomplete")
    write_finished = _validate_checkpoint_probe_execution(
        live_objects,
        checkpoint,
        jobs["write"],
        context,
        mode="write",
    )
    read_finished = _validate_checkpoint_probe_execution(
        live_objects,
        checkpoint,
        jobs["read"],
        context,
        mode="read",
    )
    if read_finished <= write_finished:
        raise ReceiptError("snapshot checkpoint remount-read proof did not finish after the writer")


def _validate_live_observations(
    client: KubeClient,
    objects: list[dict[str, Any]],
    inventories: list[dict[str, Any]],
    state: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    live_inventory_projections: list[dict[str, Any]] = []
    live_inventory_objects: list[dict[str, Any]] = []
    live_inventory_collections: list[dict[str, Any]] = []
    baseline_incompatible_objects = 0
    restricted_incompatible_objects = 0
    reference_host_paths = 0
    live_objects: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    legacy_controller_objects: list[str] = []
    for expected in objects:
        live = client.get_object(
            expected["api_version"],
            expected["kind"],
            expected["namespace"],
            expected["name"],
        )
        if not expected["exists"]:
            if live is not None:
                raise ReceiptError(f"expected absent object is present: {_object_key(expected)}")
            continue
        if live is None:
            raise ReceiptError(f"expected live object is absent: {_object_key(expected)}")
        metadata = _object(live.get("metadata"), "live object metadata")
        if (
            live.get("apiVersion") != expected["api_version"]
            or live.get("kind") != expected["kind"]
            or metadata.get("name") != expected["name"]
            or metadata.get("namespace", "") != expected["namespace"]
            or metadata.get("uid") != expected["uid"]
            or metadata.get("resourceVersion") != expected["resource_version"]
            or _sha256(_canonical(_live_projection(live))) != expected["object_sha256"]
        ):
            raise ReceiptError(f"live UID/resourceVersion/object hash differs: {_object_key(expected)}")
        live_objects[(expected["api_version"], expected["kind"], expected["namespace"], expected["name"])] = live

    for expected in inventories:
        collection = client.list_collection(
            expected["api_version"],
            expected["kind"],
            expected["namespace"],
            expected["label_selector"],
            expected["field_selector"],
        )
        collection_metadata = _object(collection.get("metadata"), "live inventory metadata")
        items = collection.get("items")
        if not isinstance(items, list):
            raise ReceiptError("live inventory items are malformed")
        if collection_metadata.get("resourceVersion") != expected["resource_version"]:
            raise ReceiptError(
                f"live inventory resourceVersion differs: "
                f"{expected['api_version']}/{expected['kind']}/{expected['namespace']}"
            )
        projections = sorted(
            (_live_projection(value) for value in items),
            key=lambda value: (
                value["metadata"]["namespace"],
                value["metadata"]["name"],
                value["metadata"]["uid"],
            ),
        )
        if len(projections) != expected["item_count"] or _sha256(_canonical(projections)) != expected["items_sha256"]:
            raise ReceiptError(
                f"live inventory differs: {expected['api_version']}/{expected['kind']}/{expected['namespace']}"
            )
        live_inventory_projections.extend(projections)
        live_inventory_collections.append(
            {
                "api_version": expected["api_version"],
                "kind": expected["kind"],
                "namespace": expected["namespace"],
                "resource_version": expected["resource_version"],
                "item_count": len(items),
            }
        )
        for item in items:
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
            name = str(metadata.get("name", "")) if isinstance(metadata, dict) else ""
            namespace = str(metadata.get("namespace", expected["namespace"])) if isinstance(metadata, dict) else ""
            projection = _live_projection(item)
            live_inventory_objects.append(
                {
                    "api_version": str(item.get("apiVersion", expected["api_version"])),
                    "kind": str(item.get("kind", expected["kind"])),
                    "namespace": namespace,
                    "name": name,
                    "uid": str(metadata.get("uid", "")),
                    "resource_version": str(metadata.get("resourceVersion", "")),
                    "object_sha256": _sha256(_canonical(projection)),
                }
            )
            live_objects[(expected["api_version"], expected["kind"], namespace, name)] = item
            controller_owned = (
                namespace == "fs2-models"
                and item.get("kind") in {"ServiceAccount", "DaemonSet"}
                and isinstance(labels, dict)
                and labels.get("app.kubernetes.io/managed-by") == "fs2-model-controller"
            )
            legacy_policy = (
                namespace == "fs2-models"
                and item.get("kind") == "NetworkPolicy"
                and isinstance(labels, dict)
                and labels.get("app.kubernetes.io/part-of") == "fs2-serve"
                and labels.get("app.kubernetes.io/managed-by") != "terraform"
                and not name.startswith("fs2-network-profile-")
            )
            if controller_owned or legacy_policy:
                legacy_controller_objects.append(
                    "/".join((str(item.get("apiVersion", "")), str(item.get("kind", "")), namespace, name))
                )
            template_findings = [
                _pod_security_findings(spec, annotations) for spec, annotations in _pod_templates(item)
            ]
            if any(result[0] for result in template_findings):
                baseline_incompatible_objects += 1
            if any(result[1] for result in template_findings):
                restricted_incompatible_objects += 1
            reference_host_paths += sum(result[2] for result in template_findings)
    live_inventory_projections.sort(
        key=lambda value: (
            str(value.get("apiVersion", "")),
            str(value.get("kind", "")),
            str(value.get("metadata", {}).get("namespace", "")),
            str(value.get("metadata", {}).get("name", "")),
            str(value.get("metadata", {}).get("uid", "")),
        )
    )
    live_inventory_objects.sort(
        key=lambda value: (value["namespace"], value["api_version"], value["kind"], value["name"])
    )
    live_inventory_collections.sort(
        key=lambda value: (value["namespace"], value["api_version"], value["kind"])
    )
    if state in {"exception-ready", "host-agents-restored"}:
        locations = ("legacy", "exception") if state == "exception-ready" else ("legacy",)
        for identity in context["host_agents"]:
            for location in locations:
                item = identity[location]
                live = live_objects.get(("apps/v1", "DaemonSet", item["namespace"], item["name"]))
                if live is None:
                    raise ReceiptError("host-agent readiness object is absent from the exact live reads")
                metadata = _object(live.get("metadata"), "host-agent metadata")
                status = _object(live.get("status"), "host-agent status")
                desired = _integer(status.get("desiredNumberScheduled"), "host-agent desiredNumberScheduled", 1)
                if (
                    status.get("observedGeneration") != metadata.get("generation")
                    or status.get("updatedNumberScheduled") != desired
                    or status.get("numberReady") != desired
                    or status.get("numberAvailable") != desired
                    or int(status.get("numberUnavailable", 0) or 0) != 0
                ):
                    raise ReceiptError("a host-agent DaemonSet is not fully rolled out and Ready")

    if state == "exception-ready":
        for config in context["host_agent_configs"]:
            live = live_objects.get(("v1", "ConfigMap", config["namespace"], config["name"]))
            if live is None:
                raise ReceiptError("host-agent config is absent from the exact live reads")
            if live.get("immutable") is not True:
                raise ReceiptError("host-agent config is not immutable")
            data = _object(live.get("data"), "host-agent config data")
            if _sha256(_canonical(data)) != config["data_sha256"]:
                raise ReceiptError("host-agent config content differs from the signed context")

    if state == "reference-data-ready":
        tree = context["dataset"]["tree_sha256"]
        pvc = live_objects.get(("v1", "PersistentVolumeClaim", "fs2-reference-data", "fs2-reference-data-rwx"))
        storage_class = live_objects.get(("storage.k8s.io/v1", "StorageClass", "", "fs2-reference-data-retained-sc"))
        probe = live_objects.get(
            (
                "batch/v1",
                "Job",
                "fs2-reference-data",
                _probe_name("fs2-reference-data-read-probe", context),
            )
        )
        if pvc is None or storage_class is None or probe is None:
            raise ReceiptError("reference-data readiness objects are absent from the exact live reads")
        pvc_metadata = _object(pvc.get("metadata"), "reference-data PVC metadata")
        pvc_spec = _object(pvc.get("spec"), "reference-data PVC spec")
        pvc_status = _object(pvc.get("status"), "reference-data PVC status")
        requests = _object(_object(pvc_spec.get("resources"), "PVC resources").get("requests"), "PVC requests")
        if (
            pvc_metadata.get("uid") != context["pvc"]["uid"]
            or pvc_metadata.get("resourceVersion") != context["pvc"]["resource_version"]
            or pvc_spec.get("volumeName") != context["pvc"]["volume_name"]
            or pvc_spec.get("storageClassName") != context["pvc"]["storage_class"]
            or pvc_spec.get("accessModes") != ["ReadWriteMany"]
            or requests.get("storage") != f"{context['storage']['claim_size_gib']}Gi"
            or pvc_status.get("phase") != "Bound"
        ):
            raise ReceiptError("retained reference-data PVC is not the exact bound RWX claim")
        storage_spec = _object(storage_class, "reference-data StorageClass")
        if storage_spec.get("reclaimPolicy") != "Retain":
            raise ReceiptError("reference-data StorageClass is not retained")
        probe_spec = _object(probe.get("spec"), "reference-data probe spec")
        probe_status = _object(probe.get("status"), "reference-data probe status")
        template_spec = _object(
            _object(_object(probe_spec.get("template"), "probe template").get("spec"), "probe pod spec"),
            "probe pod spec",
        )
        volumes = template_spec.get("volumes", [])
        if not isinstance(volumes, list):
            raise ReceiptError("reference-data read-probe volumes are malformed")
        read_only_claim = any(
            isinstance(volume, dict)
            and volume.get("name") == "reference-data"
            and isinstance(volume.get("persistentVolumeClaim"), dict)
            and volume["persistentVolumeClaim"].get("claimName") == "fs2-reference-data-rwx"
            and volume["persistentVolumeClaim"].get("readOnly") is True
            for volume in volumes
        )
        complete = any(
            isinstance(condition, dict) and condition.get("type") == "Complete" and condition.get("status") == "True"
            for condition in probe_status.get("conditions", []) or []
        )
        if (
            template_spec.get("serviceAccountName") != "fs2-reference-data"
            or template_spec.get("automountServiceAccountToken") is not False
            or not read_only_claim
            or int(probe_status.get("succeeded", 0) or 0) != 1
            or not complete
        ):
            raise ReceiptError("reference-data CSI read probe is not exactly completed and read-only")
        _validate_read_probe_execution(
            live_objects,
            pvc,
            probe,
            context,
            label="reference-data CSI read probe",
            claim_name="fs2-reference-data-rwx",
        )
        _validate_storage_successors(live_objects, context)

    baseline_namespaces = {
        "fs2-data",
        "fs2-models",
        "fs2-observability",
        "fs2-reference-data",
        "fs2-system",
        *context["scientific_namespaces"],
    }
    if state in {
        "baseline-captured",
        "cleanup-complete",
        "enforcement-quiesced",
        "baseline-enforced",
        "enforcement-removed",
        "host-agents-restored",
    }:
        for namespace in baseline_namespaces:
            live = live_objects.get(("v1", "Namespace", "", namespace))
            if live is None:
                raise ReceiptError("baseline namespace is absent from the exact live reads")
            labels = _object(_object(live.get("metadata"), "namespace metadata").get("labels", {}), "namespace labels")
            psa = {
                key: labels.get(f"pod-security.kubernetes.io/{key}")
                for key in (
                    "enforce",
                    "enforce-version",
                    "audit",
                    "audit-version",
                    "warn",
                    "warn-version",
                )
            }
            if state == "baseline-enforced":
                expected = {
                    "enforce": "baseline",
                    "enforce-version": context["psa_version"],
                    "audit": "restricted",
                    "audit-version": context["psa_version"],
                    "warn": "restricted",
                    "warn-version": context["psa_version"],
                }
                if psa != expected:
                    raise ReceiptError("a baseline namespace lacks the exact pinned PSA labels")
            elif psa["enforce"] is not None or psa["enforce-version"] is not None:
                raise ReceiptError("baseline enforcement exists before its authorized phase or after removal")

    return {
        "live_inventory_sha256": _sha256(_canonical(live_inventory_projections)),
        "live_inventory_objects": live_inventory_objects,
        "live_inventory_collections": live_inventory_collections,
        "live_reference_host_paths": reference_host_paths,
        "live_baseline_incompatible_objects": baseline_incompatible_objects,
        "live_restricted_incompatible_objects": restricted_incompatible_objects,
        "live_legacy_controller_objects": sorted(set(legacy_controller_objects)),
    }


def _validate_bundle(
    bundle: dict[str, Any],
    query: dict[str, Any],
    public_key_bytes: bytes,
    now: dt.datetime,
    *,
    allow_expired_resume: bool = False,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    str,
]:
    _exact_keys(bundle, {"schema", "authority", "context", "transition", "observations", "signature"}, "receipt bundle")
    if bundle["schema"] != BUNDLE_SCHEMA:
        raise ReceiptError("receipt bundle schema is unsupported")
    authority = _object(bundle["authority"], "authority")
    _exact_keys(authority, {"algorithm", "key_id", "signer_identity", "public_key_sha256"}, "authority")
    if authority != {
        "algorithm": "ed25519",
        "key_id": query["expected_key_id"],
        "signer_identity": query["expected_signer_identity"],
        "public_key_sha256": query["public_key_sha256"],
    }:
        raise ReceiptError("signed authority differs from the reviewed signer identity")
    _verify_signature(bundle, public_key_bytes, query["expected_key_id"])

    context = _object(bundle["context"], "context")
    _validate_context(context, query["expected_context"])
    transition = _object(bundle["transition"], "transition")
    _exact_keys(
        transition,
        {
            "receipt_id",
            "sequence",
            "from_state",
            "to_state",
            "authorizes_phase",
            "nonce",
            "prior_ledger_sha256",
            "issued_at",
            "expires_at",
        },
        "transition",
    )
    phase = query["expected_phase"]
    expected_from, expected_to = PHASE_TRANSITIONS[phase]
    if (
        transition["authorizes_phase"] != phase
        or transition["from_state"] != expected_from
        or transition["to_state"] != expected_to
    ):
        raise ReceiptError("signed transition does not match the exact phase edge")
    for field in ("receipt_id", "nonce"):
        if not IDENTIFIER_RE.fullmatch(_string(transition[field], f"transition.{field}")):
            raise ReceiptError(f"transition.{field} is malformed")
    _integer(transition["sequence"], "transition.sequence", 1)
    if not SHA256_RE.fullmatch(_string(transition["prior_ledger_sha256"], "transition.prior_ledger_sha256")):
        raise ReceiptError("transition.prior_ledger_sha256 is malformed")
    issued = _instant(transition["issued_at"], "transition.issued_at")
    expires = _instant(transition["expires_at"], "transition.expires_at")
    observed = _instant(bundle["observations"].get("observed_at"), "observations.observed_at")
    if (
        issued > now + MAX_CLOCK_SKEW
        or (expires <= now and not allow_expired_resume)
        or expires - issued > MAX_BUNDLE_AGE
    ):
        raise ReceiptError("bundle is future-dated, expired, or valid for more than 15 minutes")
    if observed > now + MAX_CLOCK_SKEW or (now - observed > MAX_OBSERVATION_AGE and not allow_expired_resume):
        raise ReceiptError("live observations are future-dated or older than two minutes")
    if observed < issued - MAX_CLOCK_SKEW or observed > expires:
        raise ReceiptError("live observation time is outside the signed transition window")
    objects, inventories, assertions = _validate_observation_contract(
        OBSERVATION_STATES[phase],
        _object(bundle["observations"], "observations"),
        context,
    )
    return transition, objects, inventories, assertions, _sha256(_canonical(bundle))


def _initial_ledger(query: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": LEDGER_SCHEMA,
        "context_sha256": _sha256(_canonical(query["expected_context"])),
        "authority": {
            "key_id": query["expected_key_id"],
            "signer_identity": query["expected_signer_identity"],
            "public_key_sha256": query["public_key_sha256"],
        },
        "sequence": 0,
        "state": "unmanaged",
        "last_bundle_sha256": None,
        "last_receipt_id": None,
        "last_nonce": None,
        "authorization": None,
    }


def _ledger_from_config_map(value: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    metadata = _object(value.get("metadata"), "ledger ConfigMap metadata")
    if (
        value.get("apiVersion") != "v1"
        or value.get("kind") != "ConfigMap"
        or metadata.get("namespace") != query["ledger_namespace"]
        or metadata.get("name") != query["ledger_name"]
        or not metadata.get("resourceVersion")
    ):
        raise ReceiptError("durable ledger ConfigMap identity is invalid")
    data = _object(value.get("data"), "ledger ConfigMap data")
    if set(data) != LEDGER_DATA_KEYS or not all(isinstance(item, str) for item in data.values()):
        raise ReceiptError("durable ledger ConfigMap has unexpected keys")
    try:
        sequence = int(data["sequence"])
    except ValueError as error:
        raise ReceiptError("durable ledger sequence is invalid") from error
    authorization = None
    if any(
        data[field]
        for field in (
            "authorization_phase",
            "authorization_bundle_sha256",
            "authorization_nonce",
            "authorization_owner_acknowledged",
            "authorization_downstream_acknowledged",
        )
    ):
        if data["authorization_owner_acknowledged"] not in {"true", "false"} or data[
            "authorization_downstream_acknowledged"
        ] not in {"true", "false"}:
            raise ReceiptError("durable ledger acknowledgement flag is invalid")
        authorization = {
            "phase": data["authorization_phase"],
            "bundle_sha256": data["authorization_bundle_sha256"],
            "nonce": data["authorization_nonce"],
            "owner_acknowledged": data["authorization_owner_acknowledged"] == "true",
            "downstream_acknowledged": data["authorization_downstream_acknowledged"] == "true",
        }
    ledger = {
        "schema": data["schema"],
        "context_sha256": data["context_sha256"],
        "authority": {
            "key_id": data["authority_key_id"],
            "signer_identity": data["authority_signer_identity"],
            "public_key_sha256": data["authority_public_key_sha256"],
        },
        "sequence": sequence,
        "state": data["state"],
        "last_bundle_sha256": data["last_bundle_sha256"] or None,
        "last_receipt_id": data["last_receipt_id"] or None,
        "last_nonce": data["last_nonce"] or None,
        "authorization": authorization,
    }
    _exact_keys(
        ledger,
        {
            "schema",
            "context_sha256",
            "authority",
            "sequence",
            "state",
            "last_bundle_sha256",
            "last_receipt_id",
            "last_nonce",
            "authorization",
        },
        "ledger",
    )
    initial = _initial_ledger(query)
    if (
        ledger["schema"] != LEDGER_SCHEMA
        or ledger["context_sha256"] != initial["context_sha256"]
        or ledger["authority"] != initial["authority"]
    ):
        raise ReceiptError("durable ledger context or authority differs")
    _integer(ledger["sequence"], "ledger.sequence")
    return ledger


def _ledger_data(ledger: dict[str, Any]) -> dict[str, str]:
    authorization = ledger.get("authorization")
    if authorization is None:
        authorization = {
            "phase": "",
            "bundle_sha256": "",
            "nonce": "",
            "owner_acknowledged": "",
            "downstream_acknowledged": "",
        }
    else:
        authorization = {
            **authorization,
            "owner_acknowledged": "true" if authorization["owner_acknowledged"] else "false",
            "downstream_acknowledged": "true" if authorization["downstream_acknowledged"] else "false",
        }
    return {
        "schema": ledger["schema"],
        "context_sha256": ledger["context_sha256"],
        "authority_key_id": ledger["authority"]["key_id"],
        "authority_signer_identity": ledger["authority"]["signer_identity"],
        "authority_public_key_sha256": ledger["authority"]["public_key_sha256"],
        "sequence": str(ledger["sequence"]),
        "state": ledger["state"],
        "last_bundle_sha256": ledger["last_bundle_sha256"] or "",
        "last_receipt_id": ledger["last_receipt_id"] or "",
        "last_nonce": ledger["last_nonce"] or "",
        "authorization_phase": authorization["phase"],
        "authorization_bundle_sha256": authorization["bundle_sha256"],
        "authorization_nonce": authorization["nonce"],
        "authorization_owner_acknowledged": authorization["owner_acknowledged"],
        "authorization_downstream_acknowledged": authorization["downstream_acknowledged"],
    }


def _validate_baseline_live_inventory(
    baseline: dict[str, Any],
    live_inventory: dict[str, Any],
    context: dict[str, Any],
) -> None:
    expected_objects = sorted(
        [
            {
                "api_version": item["api_version"],
                "kind": item["kind"],
                "namespace": item["namespace"],
                "name": item["name"],
                "uid": item["uid"],
                "resource_version": item["resource_version"],
                "object_sha256": item["object_sha256"],
            }
            for item in baseline["objects"]
        ],
        key=lambda value: (value["namespace"], value["api_version"], value["kind"], value["name"]),
    )
    expected_collections = sorted(
        [
            {
                "api_version": item["api_version"],
                "kind": item["kind"],
                "namespace": item["namespace"],
                "resource_version": item["resource_version"],
                "item_count": item["item_count"],
            }
            for item in baseline["collections"]
        ],
        key=lambda value: (value["namespace"], value["api_version"], value["kind"]),
    )
    expected_legacy = sorted(
        "/".join((item["api_version"], item["kind"], item["namespace"], item["name"]))
        for item in baseline["legacy_controller_objects"]
    )
    if (
        live_inventory["live_inventory_objects"] != expected_objects
        or live_inventory["live_inventory_collections"] != expected_collections
        or live_inventory["live_reference_host_paths"] != context["baseline"]["reference_host_paths"]
        or live_inventory["live_baseline_incompatible_objects"]
        != context["baseline"]["baseline_incompatible_objects"]
        or live_inventory["live_restricted_incompatible_objects"]
        != context["baseline"]["restricted_incompatible_objects"]
        or live_inventory["live_legacy_controller_objects"] != expected_legacy
    ):
        raise ReceiptError("immediate live inventory differs from the signed v4 baseline")


def _replace_ledger(
    client: KubeClient,
    config_map: dict[str, Any],
    ledger: dict[str, Any],
) -> None:
    replacement = deepcopy(config_map)
    replacement["data"] = _ledger_data(ledger)
    try:
        client.replace_config_map(replacement)
    except ConflictError:
        raise
    except ReceiptError:
        raise
    except Exception as error:  # pragma: no cover - defensive adapter boundary
        raise ReceiptError("durable ledger compare-and-swap failed") from error


def _daemonset_is_ready(value: dict[str, Any]) -> bool:
    metadata = value.get("metadata", {})
    status = value.get("status", {})
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        return False
    desired = status.get("desiredNumberScheduled")
    return (
        isinstance(desired, int)
        and not isinstance(desired, bool)
        and desired >= 1
        and status.get("observedGeneration") == metadata.get("generation")
        and status.get("updatedNumberScheduled") == desired
        and status.get("numberReady") == desired
        and status.get("numberAvailable") == desired
        and int(status.get("numberUnavailable", 0) or 0) == 0
    )


def _validate_reference_data_postcondition(
    client: KubeClient,
    context: dict[str, Any],
    *,
    include_successors: bool = False,
) -> None:
    pvc = client.get_object("v1", "PersistentVolumeClaim", "fs2-reference-data", "fs2-reference-data-rwx")
    storage_class = client.get_object("storage.k8s.io/v1", "StorageClass", "", "fs2-reference-data-retained-sc")
    tree = context["dataset"]["tree_sha256"]
    probe = client.get_object(
        "batch/v1",
        "Job",
        "fs2-reference-data",
        _probe_name("fs2-reference-data-read-probe", context),
    )
    if pvc is None or storage_class is None or probe is None:
        raise ReceiptError("reference-data acknowledgement objects are absent")
    pvc_metadata = _object(pvc.get("metadata"), "reference-data PVC metadata")
    pvc_spec = _object(pvc.get("spec"), "reference-data PVC spec")
    pvc_status = _object(pvc.get("status"), "reference-data PVC status")
    requests = _object(_object(pvc_spec.get("resources"), "PVC resources").get("requests"), "PVC requests")
    if (
        pvc_metadata.get("uid") != context["pvc"]["uid"]
        or pvc_metadata.get("resourceVersion") != context["pvc"]["resource_version"]
        or pvc_spec.get("volumeName") != context["pvc"]["volume_name"]
        or pvc_spec.get("storageClassName") != "fs2-reference-data-retained-sc"
        or pvc_spec.get("accessModes") != ["ReadWriteMany"]
        or requests.get("storage") != f"{context['storage']['claim_size_gib']}Gi"
        or pvc_status.get("phase") != "Bound"
        or storage_class.get("reclaimPolicy") != "Retain"
    ):
        raise ReceiptError("reference-data postcondition is not the exact retained Bound RWX claim")
    probe_status = _object(probe.get("status"), "reference-data probe status")
    if int(probe_status.get("succeeded", 0) or 0) != 1 or not any(
        isinstance(condition, dict) and condition.get("type") == "Complete" and condition.get("status") == "True"
        for condition in probe_status.get("conditions", []) or []
    ):
        raise ReceiptError("reference-data postcondition lacks a completed read probe")

    live_objects: dict[tuple[str, str, str, str], dict[str, Any]] = {
        ("v1", "PersistentVolumeClaim", "fs2-reference-data", "fs2-reference-data-rwx"): pvc,
        ("batch/v1", "Job", "fs2-reference-data", _probe_name("fs2-reference-data-read-probe", context)): probe,
    }
    tools_name = context["storage_evidence"]["tools_config_map"]
    tools = client.get_object("v1", "ConfigMap", "fs2-reference-data", tools_name)
    if tools is None:
        raise ReceiptError("reference-data acknowledgement tooling ConfigMap is absent")
    live_objects[("v1", "ConfigMap", "fs2-reference-data", tools_name)] = tools

    def read_pods(namespace: str) -> None:
        collection = client.list_collection("v1", "Pod", namespace)
        items = collection.get("items")
        if not isinstance(items, list):
            raise ReceiptError(f"Pod collection is malformed in {namespace}")
        for item_raw in items:
            item = _object(item_raw, f"Pod in {namespace}")
            metadata = _object(item.get("metadata"), f"Pod metadata in {namespace}")
            name = _string(metadata.get("name"), f"Pod name in {namespace}")
            live_objects[("v1", "Pod", namespace, name)] = item

    read_pods("fs2-reference-data")
    _validate_read_probe_execution(
        live_objects,
        pvc,
        probe,
        context,
        label="reference-data acknowledgement read probe",
        claim_name="fs2-reference-data-rwx",
    )
    if not include_successors:
        return

    def read(api_version: str, kind: str, namespace: str, name: str) -> dict[str, Any]:
        value = client.get_object(api_version, kind, namespace, name)
        if value is None:
            raise ReceiptError(f"storage successor acknowledgement object is absent: {namespace}/{name}")
        live_objects[(api_version, kind, namespace, name)] = value
        return value

    read("storage.k8s.io/v1", "StorageClass", "", REFERENCE_SUCCESSOR_STORAGE_CLASS)
    read("storage.k8s.io/v1", "StorageClass", "", SNAPSHOT_CHECKPOINT_STORAGE_CLASS)
    read(
        "v1",
        "PersistentVolume",
        "",
        context["successor_storage"]["reference_source"]["persistent_volume_name"],
    )
    for namespace, name in REFERENCE_SUCCESSOR_CLAIMS:
        read("v1", "PersistentVolume", "", REFERENCE_SUCCESSOR_VOLUMES[(namespace, name)])
        read("v1", "PersistentVolumeClaim", namespace, name)
        read("v1", "ConfigMap", namespace, tools_name)
        read(
            "batch/v1",
            "Job",
            namespace,
            _probe_name(f"{name}-read-probe", context),
        )
        read_pods(namespace)
    checkpoint_namespace, checkpoint_name = SNAPSHOT_CHECKPOINT_CLAIM
    read("v1", "PersistentVolume", "", SNAPSHOT_CHECKPOINT_VOLUME)
    read("v1", "PersistentVolumeClaim", checkpoint_namespace, checkpoint_name)
    for mode in ("write", "read"):
        read("batch/v1", "Job", checkpoint_namespace, _checkpoint_probe_name(mode, context))
    read_pods(checkpoint_namespace)
    _validate_storage_successors(live_objects, context)


def _validate_phase_acknowledgement(
    client: KubeClient,
    phase: str,
    context: dict[str, Any],
    scope: str,
) -> None:
    if phase == "bootstrap-baseline":
        for kind in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"):
            if (
                client.get_object(
                    "admissionregistration.k8s.io/v1",
                    kind,
                    "",
                    "fs2-pod-security-enforcement-fence",
                )
                is None
            ):
                raise ReceiptError("baseline bootstrap acknowledgement lacks the enforcement fence")
        return

    if phase == "migrate-reference-data":
        for identity in context["host_agents"]:
            item = identity["exception"]
            live = client.get_object("apps/v1", "DaemonSet", item["namespace"], item["name"])
            if live is None or not _daemonset_is_ready(live):
                raise ReceiptError("exception host-agent acknowledgement is not Ready")
        for config in context["host_agent_configs"]:
            live = client.get_object("v1", "ConfigMap", config["namespace"], config["name"])
            if live is None or live.get("immutable") is not True:
                raise ReceiptError("host-agent config acknowledgement is absent or mutable")
            data = _object(live.get("data"), "host-agent config data")
            if _sha256(_canonical(data)) != config["data_sha256"]:
                raise ReceiptError("host-agent config acknowledgement content differs")
        if scope == "downstream":
            _validate_reference_data_postcondition(client, context)
        return

    if phase == "cleanup-legacy-resources":
        _validate_reference_data_postcondition(
            client,
            context,
            include_successors=scope == "downstream",
        )
        return

    if phase == "quiesce-enforcement":
        _validate_reference_data_postcondition(client, context)
        return

    baseline_namespaces = {
        "fs2-data",
        "fs2-models",
        "fs2-observability",
        "fs2-system",
    }
    if scope == "downstream":
        baseline_namespaces.update({"fs2-reference-data", *context["scientific_namespaces"]})
    if phase in {"enforce", "rollback-remove-enforcement"}:
        for namespace in baseline_namespaces:
            live = client.get_object("v1", "Namespace", "", namespace)
            if live is None:
                raise ReceiptError("PSA acknowledgement namespace is absent")
            labels = _object(_object(live.get("metadata"), "namespace metadata").get("labels", {}), "namespace labels")
            actual = {
                key: labels.get(f"pod-security.kubernetes.io/{key}")
                for key in (
                    "enforce",
                    "enforce-version",
                    "audit",
                    "audit-version",
                    "warn",
                    "warn-version",
                )
            }
            if phase == "enforce":
                expected = {
                    "enforce": "baseline",
                    "enforce-version": context["psa_version"],
                    "audit": "restricted",
                    "audit-version": context["psa_version"],
                    "warn": "restricted",
                    "warn-version": context["psa_version"],
                }
                if actual != expected:
                    raise ReceiptError("PSA enforcement acknowledgement differs")
            elif actual["enforce"] is not None or actual["enforce-version"] is not None:
                raise ReceiptError("PSA rollback acknowledgement retains enforcement")
        return

    if phase == "rollback-restore-host-agents":
        for identity in context["host_agents"]:
            item = identity["legacy"]
            live = client.get_object("apps/v1", "DaemonSet", item["namespace"], item["name"])
            if live is None or not _daemonset_is_ready(live):
                raise ReceiptError("legacy host-agent acknowledgement is not Ready")
        return

    if phase == "rollback-remove-exception" and scope == "downstream":
        namespace = client.get_object("v1", "Namespace", "", "fs2-node-observability")
        if namespace is None:
            raise ReceiptError("retained exception namespace is absent after rollback acknowledgement")
        labels = _object(
            _object(namespace.get("metadata"), "retained exception namespace metadata").get("labels", {}),
            "retained exception namespace labels",
        )
        if labels.get("security.fs2.nebius.ai/host-agent-only") != "true":
            raise ReceiptError("retained exception namespace lost its admission selector")
        for identity in context["host_agents"]:
            item = identity["exception"]
            if client.get_object("apps/v1", "DaemonSet", item["namespace"], item["name"]) is not None:
                raise ReceiptError("exception host agent remains active after rollback acknowledgement")
        for config in context["host_agent_configs"]:
            live = client.get_object("v1", "ConfigMap", config["namespace"], config["name"])
            if live is None or live.get("immutable") is not True:
                raise ReceiptError("retained host-agent config is absent or mutable after rollback")
            if _sha256(_canonical(_object(live.get("data"), "retained host-agent config data"))) != config[
                "data_sha256"
            ]:
                raise ReceiptError("retained host-agent config content differs after rollback")


def verify_and_consume(query: dict[str, Any], client: KubeClient, now: dt.datetime | None = None) -> dict[str, str]:
    _exact_keys(
        query,
        {
            "mode",
            "receipt_path",
            "public_key_path",
            "public_key_sha256",
            "expected_key_id",
            "expected_signer_identity",
            "expected_context",
            "expected_phase",
            "baseline_artifact_path",
            "cleanup_result_path",
            "ledger_namespace",
            "ledger_name",
        },
        "query",
    )
    mode = query["mode"]
    if mode not in {
        "owner-transition",
        "owner-acknowledgement",
        "downstream-authorization",
        "downstream-acknowledgement",
    }:
        raise ReceiptError("query.mode is unsupported")
    phase = _string(query["expected_phase"], "expected_phase")
    if phase not in PHASE_TRANSITIONS:
        raise ReceiptError("prepare has no consumable receipt; expected_phase is unsupported")
    for field in ("expected_key_id", "expected_signer_identity", "ledger_name", "ledger_namespace"):
        if not IDENTIFIER_RE.fullmatch(_string(query[field], field)):
            raise ReceiptError(f"{field} is malformed")
    key_digest = _string(query["public_key_sha256"], "public_key_sha256")
    if not SHA256_RE.fullmatch(key_digest):
        raise ReceiptError("public_key_sha256 is malformed")
    public_key = Path(_string(query["public_key_path"], "public_key_path"))
    key_bytes = _read_regular_file(public_key, "receipt public key", 65536)
    if _sha256(key_bytes) != key_digest:
        raise ReceiptError("receipt public key digest differs from the reviewed digest")
    receipt_path = Path(_string(query["receipt_path"], "receipt_path"))
    bundle = _load_json(receipt_path)
    ledger_config_map = client.get_object("v1", "ConfigMap", query["ledger_namespace"], query["ledger_name"])
    if ledger_config_map is None:
        raise ReceiptError("durable rollout ledger is absent")
    ledger = _ledger_from_config_map(ledger_config_map, query)
    candidate_bundle_sha256 = _sha256(_canonical(bundle))
    exact_resume = ledger["last_bundle_sha256"] == candidate_bundle_sha256
    current_time = now or dt.datetime.now(dt.UTC)
    transition, objects, inventories, assertions, bundle_sha256 = _validate_bundle(
        bundle,
        query,
        key_bytes,
        current_time,
        allow_expired_resume=exact_resume,
    )
    baseline_artifact = Path(_string(query["baseline_artifact_path"], "baseline_artifact_path"))
    baseline = _validate_baseline_artifact(
        _read_regular_file(baseline_artifact, "baseline artifact", 128 * 1024 * 1024),
        _object(bundle["context"], "context"),
    )
    if phase == "quiesce-enforcement":
        cleanup_path = Path(_string(query["cleanup_result_path"], "cleanup_result_path"))
        _validate_cleanup_result(
            _read_regular_file(cleanup_path, "cleanup result", 8 * 1024 * 1024),
            assertions,
            _object(bundle["context"], "context"),
            baseline,
        )
    elif query["cleanup_result_path"] is not None:
        raise ReceiptError("cleanup_result_path is valid only for quiesce-enforcement")

    observation_state = OBSERVATION_STATES[phase]
    live_inventory: dict[str, Any] | None = None
    if mode == "owner-transition" or (
        mode in {"owner-acknowledgement", "downstream-authorization"} and observation_state == "cleanup-complete"
    ):
        live_inventory = _validate_live_observations(
            client,
            objects,
            inventories,
            observation_state,
            _object(bundle["context"], "context"),
        )
    if observation_state == "baseline-captured" and live_inventory is not None:
        _validate_baseline_live_inventory(baseline, live_inventory, _object(bundle["context"], "context"))
    if observation_state in {"cleanup-complete", "enforcement-quiesced"} and live_inventory is not None:
        expected_live = {
            "live_inventory_sha256": assertions["live_inventory_sha256"],
            "live_reference_host_paths": assertions["live_reference_host_paths"],
            "live_baseline_incompatible_objects": assertions["live_baseline_incompatible_objects"],
            "live_restricted_incompatible_objects": assertions["live_restricted_incompatible_objects"],
            "live_legacy_controller_objects": assertions["live_legacy_controller_objects"],
        }
        if any(live_inventory[field] != expected for field, expected in expected_live.items()):
            raise ReceiptError("immediate live workload inventory differs from signed clean assertions")

    authorization = ledger.get("authorization")
    exact_authorization = bool(
        isinstance(authorization, dict)
        and ledger["state"] == transition["to_state"]
        and ledger["sequence"] == transition["sequence"]
        and ledger["last_bundle_sha256"] == bundle_sha256
        and ledger["last_receipt_id"] == transition["receipt_id"]
        and ledger["last_nonce"] == transition["nonce"]
        and authorization.get("phase") == phase
        and authorization.get("bundle_sha256") == bundle_sha256
        and authorization.get("nonce") == transition["nonce"]
    )
    changed = False
    if mode == "owner-transition":
        if not exact_authorization:
            prior_authorization = ledger.get("authorization")
            if prior_authorization is not None and not (
                prior_authorization.get("owner_acknowledged") is True
                and prior_authorization.get("downstream_acknowledged") is True
            ):
                raise ReceiptError("prior phase has not been acknowledged by both Terraform stages")
            if (
                ledger["state"] != transition["from_state"]
                or ledger["sequence"] + 1 != transition["sequence"]
                or transition["prior_ledger_sha256"] != _sha256(_canonical(ledger))
            ):
                raise ReceiptError("signed transition does not extend the current durable ledger")
            if transition["receipt_id"] == ledger["last_receipt_id"] or transition["nonce"] == ledger["last_nonce"]:
                raise ReceiptError("receipt ID or nonce was already consumed by another transition")
            ledger.update(
                {
                    "sequence": transition["sequence"],
                    "state": transition["to_state"],
                    "last_bundle_sha256": bundle_sha256,
                    "last_receipt_id": transition["receipt_id"],
                    "last_nonce": transition["nonce"],
                    "authorization": {
                        "phase": phase,
                        "bundle_sha256": bundle_sha256,
                        "nonce": transition["nonce"],
                        "owner_acknowledged": False,
                        "downstream_acknowledged": False,
                    },
                }
            )
            changed = True
    else:
        if not exact_authorization:
            raise ReceiptError("durable ledger does not carry this exact phase authorization")
        authorization = _object(ledger["authorization"], "ledger.authorization")
        _exact_keys(
            authorization,
            {
                "phase",
                "bundle_sha256",
                "nonce",
                "owner_acknowledged",
                "downstream_acknowledged",
            },
            "ledger.authorization",
        )
        if mode == "owner-acknowledgement":
            _validate_phase_acknowledgement(client, phase, bundle["context"], "owner")
            if authorization["owner_acknowledged"] is not True:
                authorization["owner_acknowledged"] = True
                changed = True
        elif mode == "downstream-authorization":
            if authorization["owner_acknowledged"] is not True:
                raise ReceiptError("foundation resources have not acknowledged this authorization")
            _validate_phase_acknowledgement(client, phase, bundle["context"], "owner")
        else:
            if authorization["owner_acknowledged"] is not True:
                raise ReceiptError("foundation resources have not acknowledged this authorization")
            _validate_phase_acknowledgement(client, phase, bundle["context"], "downstream")
            if authorization["downstream_acknowledged"] is not True:
                authorization["downstream_acknowledged"] = True
                changed = True
        ledger["authorization"] = authorization

    if changed:
        _replace_ledger(client, ledger_config_map, ledger)
    return {
        "valid": "true",
        "terminal_state": transition["to_state"],
        "bundle_sha256": bundle_sha256,
        "sequence": str(transition["sequence"]),
        "consumer": mode,
    }


class KubectlClient:
    def __init__(self, kubeconfig: Path, context: str) -> None:
        if not kubeconfig.is_absolute() or ".." in kubeconfig.parts:
            raise ReceiptError("kubeconfig_path must be absolute without parent traversal")
        if not IDENTIFIER_RE.fullmatch(context):
            raise ReceiptError("kube_context is malformed")
        self._base = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            context,
            "--as=system:serviceaccount:fs2-system:fs2-pod-security-rollout-manager",
        ]

    def _raw(self, path: str) -> dict[str, Any] | None:
        completed = subprocess.run(
            [*self._base, "get", "--raw", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            lowered = completed.stderr.lower()
            if "not found" in lowered or "404" in lowered:
                return None
            raise ReceiptError(f"live Kubernetes read failed for {path}")
        try:
            return _object(json.loads(completed.stdout), "live Kubernetes response")
        except json.JSONDecodeError as error:
            raise ReceiptError("live Kubernetes response is not JSON") from error

    def get_object(self, api_version: str, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        return self._raw(_resource_path(api_version, kind, namespace, name))

    def list_collection(
        self,
        api_version: str,
        kind: str,
        namespace: str,
        label_selector: str,
        field_selector: str,
    ) -> dict[str, Any]:
        query = urlencode(
            {
                key: value
                for key, value in {"labelSelector": label_selector, "fieldSelector": field_selector}.items()
                if value
            }
        )
        path = _resource_path(api_version, kind, namespace)
        value = self._raw(f"{path}?{query}" if query else path)
        if (
            value is None
            or not isinstance(value.get("items"), list)
            or not isinstance(value.get("metadata"), dict)
            or not value["metadata"].get("resourceVersion")
        ):
            raise ReceiptError("live Kubernetes collection response is malformed")
        return value

    def replace_config_map(self, value: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            [*self._base, "replace", "-f", "-"],
            input=_canonical(value),
            check=False,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").lower()
            if "conflict" in stderr or "the object has been modified" in stderr:
                raise ConflictError("durable ledger changed during compare-and-swap")
            raise ReceiptError("durable ledger replace failed")
        try:
            return _object(json.loads(completed.stdout), "ledger replace response")
        except json.JSONDecodeError as error:
            raise ReceiptError("ledger replace response is not JSON") from error


def _query_from_environment() -> dict[str, Any]:
    raw = os.environ.get("FS2_POD_SECURITY_QUERY")
    if raw is None:
        return _object(json.load(sys.stdin), "query")
    try:
        return _object(json.loads(raw), "query")
    except json.JSONDecodeError as error:
        raise ReceiptError("FS2_POD_SECURITY_QUERY is not JSON") from error


def main() -> int:
    try:
        query = _query_from_environment()
        kubeconfig = Path(_string(os.environ.get("FS2_KUBECONFIG"), "FS2_KUBECONFIG"))
        kube_context = _string(os.environ.get("FS2_KUBE_CONTEXT"), "FS2_KUBE_CONTEXT")
        result = verify_and_consume(query, KubectlClient(kubeconfig, kube_context))
    except (OSError, ReceiptError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"pod-security receipt consumption failed: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
