from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = ROOT / "scripts" / "verify_pod_security_receipts.py"
SPEC = importlib.util.spec_from_file_location("sai07_receipts", VERIFIER_PATH)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)

NOW = dt.datetime(2026, 9, 16, 20, 0, tzinfo=dt.timezone.utc)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture()
def authority(tmp_path: Path) -> tuple[Path, Path]:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", private_key], check=True)
    subprocess.run(["openssl", "pkey", "-in", private_key, "-pubout", "-out", public_key], check=True)
    return private_key, public_key


@pytest.fixture()
def context() -> dict[str, object]:
    return {
        "cluster_id": "mk8scluster-test",
        "run_id": "sai07test",
        "kube_system_uid": "00000000-0000-0000-0000-000000000001",
        "deployment_nonce": "sai07-test-20260916",
        "exception_admission_sha256": "d" * 64,
        "psa_version": "v1.35",
        "scientific_namespaces": [
            "fs2-academic-poc",
            "fs2-bioir-boltz2",
            "fs2-bioir-coverage",
            "fs2-bioir-openfold",
            "fs2-bioir-protenix",
            "fs2-bioir-snapshot",
        ],
        "host_agents": [
            {
                "component": "dcgm-exporter",
                "legacy": {"namespace": "fs2-observability", "name": "fs2-dcgm-exporter"},
                "exception": {"namespace": "fs2-node-observability", "name": "fs2-dcgm-exporter"},
            },
            {
                "component": "gpu-observer",
                "legacy": {"namespace": "fs2-system", "name": "fs2-serve-control-plane-gpu-observer"},
                "exception": {
                    "namespace": "fs2-node-observability",
                    "name": "fs2-serve-control-plane-gpu-observer",
                },
            },
            {
                "component": "node-exporter",
                "legacy": {
                    "namespace": "fs2-observability",
                    "name": "fs2-sai07test-monitoring-prometheus-node-exporter",
                },
                "exception": {"namespace": "fs2-node-observability", "name": "fs2-node-exporter"},
            },
            {
                "component": "otel-node",
                "legacy": {"namespace": "fs2-observability", "name": "fs2-otel-node-agent"},
                "exception": {"namespace": "fs2-node-observability", "name": "fs2-otel-node-agent"},
            },
        ],
        "pvc": {
            "namespace": "fs2-reference-data",
            "name": "fs2-reference-data-rwx",
            "uid": "pvc-test-uid",
            "storage_class": "fs2-reference-data-retained-sc",
        },
        "dataset": {
            "id": "alphafold3-public-databases-v3.0",
            "revision": "20230311",
            "tree_sha256": "b" * 64,
        },
        "storage": {
            "filesystem_id": "computefilesystem-test",
            "capacity_gib": 2048,
            "claim_size_gib": 1611,
            "forbid_deletion": True,
            "retention_mode": "retain",
        },
    }


def live_object(
    api_version: str,
    kind: str,
    namespace: str,
    name: str,
    ordinal: int,
) -> dict[str, object]:
    value: dict[str, object] = {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{ordinal:02d}",
            "resourceVersion": str(1000 + ordinal),
            "generation": 1,
            "labels": {"app.kubernetes.io/managed-by": "terraform"},
            "annotations": {},
            "ownerReferences": [],
        },
    }
    if kind == "DaemonSet":
        value["spec"] = {"selector": {"matchLabels": {"app": name}}}
        value["status"] = {
            "observedGeneration": 1,
            "desiredNumberScheduled": 1,
            "updatedNumberScheduled": 1,
            "numberReady": 1,
            "numberAvailable": 1,
            "numberUnavailable": 0,
        }
    elif kind == "Namespace":
        value["spec"] = {"finalizers": ["kubernetes"]}
        value["status"] = {"phase": "Active"}
    else:
        value["spec"] = {}
    return value


def observation(value: dict[str, object]) -> dict[str, object]:
    metadata = value["metadata"]
    assert isinstance(metadata, dict)
    return {
        "api_version": value["apiVersion"],
        "kind": value["kind"],
        "namespace": metadata.get("namespace", ""),
        "name": metadata["name"],
        "exists": True,
        "uid": metadata["uid"],
        "resource_version": metadata["resourceVersion"],
        "object_sha256": hashlib.sha256(canonical(verifier._live_projection(value))).hexdigest(),
    }


def exception_objects() -> list[dict[str, object]]:
    identities = [
        ("v1", "Namespace", "", "fs2-node-observability"),
        *[
            ("apps/v1", "DaemonSet", "fs2-node-observability", name)
            for name in sorted(verifier.EXCEPTION_DAEMONSETS)
        ],
        *[
            ("v1", "ServiceAccount", "fs2-node-observability", name)
            for name in sorted(verifier.EXCEPTION_SERVICE_ACCOUNTS)
        ],
        (
            "admissionregistration.k8s.io/v1",
            "ValidatingAdmissionPolicy",
            "",
            "fs2-node-observability-daemonsets",
        ),
        (
            "admissionregistration.k8s.io/v1",
            "ValidatingAdmissionPolicyBinding",
            "",
            "fs2-node-observability-daemonsets",
        ),
        (
            "admissionregistration.k8s.io/v1",
            "ValidatingAdmissionPolicy",
            "",
            "fs2-node-observability-pods",
        ),
        (
            "admissionregistration.k8s.io/v1",
            "ValidatingAdmissionPolicyBinding",
            "",
            "fs2-node-observability-pods",
        ),
        *sorted(verifier.SNAPSHOT_EXCEPTION_OBJECTS),
        *[
            ("apps/v1", "DaemonSet", identity[location]["namespace"], identity[location]["name"])
            for identity in context_host_agents()
            for location in ("legacy", "exception")
        ],
    ]
    identities = sorted(set(identities))
    return [
        live_object(api_version, kind, namespace, name, ordinal)
        for ordinal, (api_version, kind, namespace, name) in enumerate(identities, start=1)
    ]


def context_host_agents() -> list[dict[str, object]]:
    return [
        {
            "legacy": {"namespace": "fs2-observability", "name": "fs2-dcgm-exporter"},
            "exception": {"namespace": "fs2-node-observability", "name": "fs2-dcgm-exporter"},
        },
        {
            "legacy": {"namespace": "fs2-system", "name": "fs2-serve-control-plane-gpu-observer"},
            "exception": {
                "namespace": "fs2-node-observability",
                "name": "fs2-serve-control-plane-gpu-observer",
            },
        },
        {
            "legacy": {
                "namespace": "fs2-observability",
                "name": "fs2-sai07test-monitoring-prometheus-node-exporter",
            },
            "exception": {"namespace": "fs2-node-observability", "name": "fs2-node-exporter"},
        },
        {
            "legacy": {"namespace": "fs2-observability", "name": "fs2-otel-node-agent"},
            "exception": {"namespace": "fs2-node-observability", "name": "fs2-otel-node-agent"},
        },
    ]


class FakeClient:
    def __init__(self, objects: list[dict[str, object]], ledger: dict[str, object]) -> None:
        self.objects: dict[tuple[str, str, str, str], dict[str, object]] = {}
        for value in objects:
            metadata = value["metadata"]
            assert isinstance(metadata, dict)
            self.objects[
                (
                    str(value["apiVersion"]),
                    str(value["kind"]),
                    str(metadata.get("namespace", "")),
                    str(metadata["name"]),
                )
            ] = deepcopy(value)
        self.ledger = deepcopy(ledger)
        self.conflict = False

    def get_object(
        self, api_version: str, kind: str, namespace: str, name: str
    ) -> dict[str, object] | None:
        if (api_version, kind, namespace, name) == (
            "v1",
            "ConfigMap",
            "fs2-system",
            "fs2-pod-security-rollout-ledger",
        ):
            return deepcopy(self.ledger)
        value = self.objects.get((api_version, kind, namespace, name))
        return deepcopy(value) if value is not None else None

    def list_objects(
        self,
        api_version: str,
        kind: str,
        namespace: str,
        label_selector: str,
        field_selector: str,
    ) -> list[dict[str, object]]:
        assert not label_selector and not field_selector
        return [
            deepcopy(value)
            for (item_api, item_kind, item_namespace, _), value in self.objects.items()
            if (item_api, item_kind, item_namespace) == (api_version, kind, namespace)
        ]

    def replace_config_map(self, value: dict[str, object]) -> dict[str, object]:
        if self.conflict:
            raise verifier.ConflictError("simulated resourceVersion conflict")
        current_metadata = self.ledger["metadata"]
        next_metadata = value["metadata"]
        assert isinstance(current_metadata, dict) and isinstance(next_metadata, dict)
        if next_metadata["resourceVersion"] != current_metadata["resourceVersion"]:
            raise verifier.ConflictError("resourceVersion differs")
        replacement = deepcopy(value)
        replacement_metadata = replacement["metadata"]
        assert isinstance(replacement_metadata, dict)
        replacement_metadata["resourceVersion"] = str(int(str(current_metadata["resourceVersion"])) + 1)
        self.ledger = replacement
        return deepcopy(replacement)


def query(public_key: Path, context: dict[str, object], mode: str = "owner-transition") -> dict[str, object]:
    inspected_namespaces = [
        "fs2-data",
        "fs2-models",
        "fs2-observability",
        "fs2-reference-data",
        "fs2-system",
        *context["scientific_namespaces"],  # type: ignore[misc]
    ]
    artifact: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/sai07-baseline-inventory/v3",
        "captured_at": NOW.isoformat().replace("+00:00", "Z"),
        "cluster": {"kube_system_uid": context["kube_system_uid"]},
        "scientific_namespaces": context["scientific_namespaces"],
        "inspected_namespaces": inspected_namespaces,
        "collections": [
            {
                "api_version": api_version,
                "kind": kind,
                "namespace": namespace,
                "resource_version": "1",
                "item_count": 0,
            }
            for namespace in inspected_namespaces
            for api_version, kind in sorted(verifier.BASELINE_INVENTORY_KINDS)
        ],
        "objects": [],
        "reference_host_paths": 103,
        "baseline_incompatible_objects": 103,
        "restricted_incompatible_objects": 716,
        "legacy_controller_objects": [],
        "unauthorized_exception_objects": [],
    }
    artifact["inventory_sha256"] = hashlib.sha256(canonical(artifact)).hexdigest()
    baseline_path = public_key.parent / "baseline.json"
    baseline_path.write_bytes(canonical(artifact))
    context["baseline"] = {
        "artifact_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "inventory_sha256": artifact["inventory_sha256"],
        "reference_host_paths": 103,
        "baseline_incompatible_objects": 103,
        "restricted_incompatible_objects": 716,
    }
    return {
        "mode": mode,
        "receipt_path": "",
        "public_key_path": str(public_key),
        "public_key_sha256": hashlib.sha256(public_key.read_bytes()).hexdigest(),
        "expected_key_id": "sai07-review-authority",
        "expected_signer_identity": "platform-security-reviewer",
        "expected_context": context,
        "expected_phase": "migrate-reference-data",
        "baseline_artifact_path": str(baseline_path),
        "cleanup_result_path": None,
        "ledger_namespace": "fs2-system",
        "ledger_name": "fs2-pod-security-rollout-ledger",
    }


def initial_ledger(query_value: dict[str, object]) -> dict[str, object]:
    ledger = verifier._initial_ledger(query_value)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "fs2-pod-security-rollout-ledger",
            "namespace": "fs2-system",
            "uid": "ledger-uid",
            "resourceVersion": "10",
        },
        "data": verifier._ledger_data(ledger),
    }


def signed_bundle(
    tmp_path: Path,
    private_key: Path,
    query_value: dict[str, object],
    ledger: dict[str, object],
    objects: list[dict[str, object]],
) -> Path:
    ledger_value = verifier._ledger_from_config_map(ledger, query_value)
    unsigned: dict[str, object] = {
        "schema": verifier.BUNDLE_SCHEMA,
        "authority": {
            "algorithm": "ed25519",
            "key_id": query_value["expected_key_id"],
            "signer_identity": query_value["expected_signer_identity"],
            "public_key_sha256": query_value["public_key_sha256"],
        },
        "context": query_value["expected_context"],
        "transition": {
            "receipt_id": "receipt-exception-ready-1",
            "sequence": 1,
            "from_state": "unmanaged",
            "to_state": "exception-ready",
            "authorizes_phase": "migrate-reference-data",
            "nonce": "nonce-exception-ready-1",
            "prior_ledger_sha256": hashlib.sha256(canonical(ledger_value)).hexdigest(),
            "issued_at": NOW.isoformat().replace("+00:00", "Z"),
            "expires_at": (NOW + dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        },
        "observations": {
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
            "objects": sorted((observation(value) for value in objects), key=verifier._object_key),
            "inventories": [],
            "assertions": {
                "legacy_agents_ready": True,
                "exception_agents_ready": True,
            },
        },
    }
    message = tmp_path / "bundle-unsigned.json"
    signature = tmp_path / "bundle.sig"
    message.write_bytes(canonical(unsigned))
    subprocess.run(
        ["openssl", "pkeyutl", "-sign", "-inkey", private_key, "-rawin", "-in", message, "-out", signature],
        check=True,
    )
    bundle = {
        **unsigned,
        "signature": {
            "algorithm": "ed25519",
            "key_id": query_value["expected_key_id"],
            "value": base64.b64encode(signature.read_bytes()).decode(),
        },
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    query_value["receipt_path"] = str(path)
    return path


def setup_case(
    tmp_path: Path,
    authority: tuple[Path, Path],
    context: dict[str, object],
) -> tuple[dict[str, object], FakeClient, Path, list[dict[str, object]]]:
    private_key, public_key = authority
    query_value = query(public_key, context)
    objects = exception_objects()
    ledger = initial_ledger(query_value)
    path = signed_bundle(tmp_path, private_key, query_value, ledger, objects)
    return query_value, FakeClient(objects, ledger), path, objects


def test_whole_bundle_live_verification_and_two_consumers_are_one_time(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    query_value, client, _, _ = setup_case(tmp_path, authority, context)
    owner = verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))
    assert owner["terminal_state"] == "exception-ready"

    with pytest.raises(verifier.ReceiptError, match="does not extend"):
        verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=6))

    downstream_query = {**query_value, "mode": "downstream-authorization"}
    downstream = verifier.verify_and_consume(
        downstream_query, client, NOW + dt.timedelta(seconds=7)
    )
    assert downstream["consumer"] == "downstream-authorization"
    with pytest.raises(verifier.ReceiptError, match="already consumed"):
        verifier.verify_and_consume(
            downstream_query, client, NOW + dt.timedelta(seconds=8)
        )


def test_unsigned_context_substitution_is_rejected_even_when_expected_context_changes(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    query_value, client, path, _ = setup_case(tmp_path, authority, context)
    replacement = deepcopy(context)
    replacement.update(
        cluster_id="mk8scluster-other",
        run_id="other-run",
        kube_system_uid="00000000-0000-0000-0000-000000000002",
        deployment_nonce="other-deployment-nonce",
    )
    replacement["pvc"] = {
        **replacement["pvc"],  # type: ignore[dict-item]
        "uid": "other-pvc-uid",
    }
    value = json.loads(path.read_text())
    value["context"] = replacement
    path.write_text(json.dumps(value))
    query_value["expected_context"] = replacement
    client.ledger = initial_ledger(query_value)

    with pytest.raises(verifier.ReceiptError, match="whole-bundle signature"):
        verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))


@pytest.mark.parametrize("field", ["resourceVersion", "spec"])
def test_stale_or_fabricated_live_object_is_rejected(
    tmp_path: Path,
    authority: tuple[Path, Path],
    context: dict[str, object],
    field: str,
) -> None:
    query_value, client, _, objects = setup_case(tmp_path, authority, context)
    daemonset = next(value for value in objects if value["kind"] == "DaemonSet")
    metadata = daemonset["metadata"]
    assert isinstance(metadata, dict)
    identity = (
        str(daemonset["apiVersion"]),
        str(daemonset["kind"]),
        str(metadata["namespace"]),
        str(metadata["name"]),
    )
    if field == "resourceVersion":
        client.objects[identity]["metadata"]["resourceVersion"] = "9999"  # type: ignore[index]
    else:
        client.objects[identity]["spec"] = {"selector": {"matchLabels": {"app": "drifted"}}}

    with pytest.raises(verifier.ReceiptError, match="live UID/resourceVersion/object hash differs"):
        verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))


def test_phase_jump_and_signer_substitution_are_rejected(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    query_value, client, path, _ = setup_case(tmp_path, authority, context)
    value = json.loads(path.read_text())
    value["transition"]["to_state"] = "baseline-ready"
    path.write_text(json.dumps(value))
    with pytest.raises(verifier.ReceiptError, match="whole-bundle signature"):
        verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))

    query_value, client, path, _ = setup_case(tmp_path, authority, context)
    value = json.loads(path.read_text())
    value["authority"]["signer_identity"] = "unreviewed-signer"
    path.write_text(json.dumps(value))
    query_value["expected_signer_identity"] = "unreviewed-signer"
    client.ledger = initial_ledger(query_value)
    with pytest.raises(verifier.ReceiptError, match="whole-bundle signature"):
        verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))


def test_stale_observation_and_atomic_ledger_conflict_are_rejected(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    private_key, public_key = authority
    query_value = query(public_key, context)
    objects = exception_objects()
    ledger = initial_ledger(query_value)
    path = signed_bundle(tmp_path, private_key, query_value, ledger, objects)
    value = json.loads(path.read_text())
    value["observations"]["observed_at"] = (
        NOW - dt.timedelta(minutes=3)
    ).isoformat().replace("+00:00", "Z")
    unsigned = dict(value)
    del unsigned["signature"]
    message = tmp_path / "stale.json"
    signature = tmp_path / "stale.sig"
    message.write_bytes(canonical(unsigned))
    subprocess.run(
        ["openssl", "pkeyutl", "-sign", "-inkey", private_key, "-rawin", "-in", message, "-out", signature],
        check=True,
    )
    value["signature"]["value"] = base64.b64encode(signature.read_bytes()).decode()
    path.write_text(json.dumps(value))
    client = FakeClient(objects, ledger)
    with pytest.raises(verifier.ReceiptError, match="older than two minutes"):
        verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))

    query_value, client, _, _ = setup_case(tmp_path, authority, context)
    client.conflict = True
    with pytest.raises(verifier.ConflictError, match="resourceVersion conflict"):
        verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))


def test_signature_uses_the_descriptor_fenced_public_key_bytes(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    private_key, public_key = authority
    query_value = query(public_key, context)
    ledger = initial_ledger(query_value)
    path = signed_bundle(tmp_path, private_key, query_value, ledger, exception_objects())
    bundle = json.loads(path.read_text())
    reviewed_bytes = public_key.read_bytes()
    public_key.write_text("replaced after descriptor-fenced read", encoding="utf-8")
    verifier._verify_signature(bundle, reviewed_bytes, "sai07-review-authority")


def test_reference_data_transition_requires_bound_retained_rwx_and_completed_read_probe(
    context: dict[str, object],
) -> None:
    tree = context["dataset"]["tree_sha256"]  # type: ignore[index]
    pvc = live_object("v1", "PersistentVolumeClaim", "fs2-reference-data", "fs2-reference-data-rwx", 1)
    pvc["metadata"]["uid"] = context["pvc"]["uid"]  # type: ignore[index]
    pvc["spec"] = {
        "accessModes": ["ReadWriteMany"],
        "storageClassName": "fs2-reference-data-retained-sc",
        "resources": {"requests": {"storage": "1611Gi"}},
    }
    pvc["status"] = {"phase": "Bound"}
    storage_class = live_object(
        "storage.k8s.io/v1", "StorageClass", "", "fs2-reference-data-retained-sc", 2
    )
    storage_class["reclaimPolicy"] = "Retain"
    probe = live_object(
        "batch/v1",
        "Job",
        "fs2-reference-data",
        f"fs2-reference-data-read-probe-{tree[:12]}",
        3,
    )
    probe["spec"] = {
        "template": {
            "spec": {
                "serviceAccountName": "fs2-reference-data",
                "automountServiceAccountToken": False,
                "volumes": [{
                    "name": "reference-data",
                    "persistentVolumeClaim": {
                        "claimName": "fs2-reference-data-rwx",
                        "readOnly": True,
                    },
                }],
            }
        }
    }
    probe["status"] = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
    objects = [pvc, storage_class, probe]
    client = FakeClient(objects, {})
    verifier._validate_live_observations(
        client,
        [observation(value) for value in objects],
        [],
        "reference-data-ready",
        context,
    )

    probe["status"] = {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}
    client = FakeClient([pvc, storage_class, probe], {})
    with pytest.raises(verifier.ReceiptError, match="read probe is not exactly completed"):
        verifier._validate_live_observations(
            client,
            [observation(pvc), observation(storage_class), observation(probe)],
            [],
            "reference-data-ready",
            context,
        )
