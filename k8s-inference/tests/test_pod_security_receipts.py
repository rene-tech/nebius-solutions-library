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

NOW = dt.datetime(2026, 9, 16, 20, 0, tzinfo=dt.UTC)


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
    probe_image = "registry.invalid/fs2/reference-data@sha256:" + "f" * 64
    proof_tools = {
        "verify_checkpoint_durability.py": "def proof() -> None:\n    return None\n",
        "verify_csi_readiness.py": "assert 0 < 1 and 'a&b'\n",
    }
    proof_generation = {
        "sequence": 1,
        "attempt": 1,
        "dataset_id": "alphafold3-public-databases-v3.0",
        "dataset_revision": "20230311",
        "dataset_tree_sha256": "b" * 64,
        "deployment_nonce": "sai07-test-20260916",
        "probe_image": probe_image,
        "tools_data": proof_tools,
        "tools_data_sha256": hashlib.sha256(verifier._terraform_canonical(proof_tools)).hexdigest(),
    }
    proof_generation_id = hashlib.sha256(verifier._terraform_canonical(proof_generation)).hexdigest()
    predecessor_resources = []
    for index, (address, (api_version, kind, namespace)) in enumerate(
        sorted(verifier.PREDECESSOR_TERRAFORM_ADDRESSES.items()), start=1
    ):
        if kind == "ConfigMap":
            name = "fs2-reference-data-tools-" + "1" * 12
        elif "checkpoint_write" in address:
            name = "fs2-snapshot-checkpoints-durability-write-" + "2" * 12
        elif "checkpoint_read" in address:
            name = "fs2-snapshot-checkpoints-durability-read-" + "2" * 12
        else:
            claim = "fs2-snapshot-reference" if namespace == "fs2-snapshot-operations" else "fs2-reference-data-rwx"
            name = f"{claim}-read-probe-{'3' * 12}-{'4' * 12}"
        retained = {
            "terraform_address": address,
            "api_version": api_version,
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "uid": f"predecessor-{index}",
            "resource_version": str(1000 + index),
            "object_sha256": "",
        }
        retained["object_sha256"] = hashlib.sha256(
            canonical(verifier._live_projection(predecessor_live_object(retained)))
        ).hexdigest()
        predecessor_resources.append(retained)
    successor_storage = {
        "schema": "fs2-serve.nebius.ai/sai07-successor-storage/v3",
        "reference_source": {
            "persistent_volume_name": "pv-reference-data-test",
            "uid": "pv-reference-data-uid",
            "resource_version": "900",
            "csi_driver": "reference-data.mounted-fs-path.csi.nebius.ai",
            "volume_handle": "reference-data-volume-test",
            "volume_attributes": {"storage.kubernetes.io/csiProvisionerIdentity": "test"},
            "capacity_quantity": "2048Gi",
            "capacity_gib": 2048,
            "provisioning_receipt_sha256": "c" * 64,
            "storage_owner": "platform-storage",
        },
        "checkpoint_source": {
            "persistent_volume_name": "fs2-sai07-snapshot-checkpoints",
            "csi_driver": "reference-data.mounted-fs-path.csi.nebius.ai",
            "volume_handle": "snapshot-checkpoints-volume-test",
            "volume_attributes": {"storage.kubernetes.io/csiProvisionerIdentity": "test"},
            "capacity_gib": 256,
            "requested_gib": 256,
            "provisioning_receipt_sha256": "d" * 64,
            "storage_owner": "platform-storage",
        },
        "predecessor_adoption": {
            "schema": "fs2-serve.nebius.ai/sai07-predecessor-adoption/v1",
            "mode": "retained-v2",
            "rollout_ledger_v2_data_sha256": "e" * 64,
            "resources": predecessor_resources,
        },
        "proof_generation_ledger": {
            "schema": "fs2-serve.nebius.ai/sai07-proof-generation-ledger/v2",
            "maximum_generations": 8,
            "active_generation": proof_generation_id,
            "generations": {proof_generation_id: proof_generation},
        },
    }
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
        "host_agent_configs": [
            {
                "component": "dcgm-cold-config",
                "namespace": "fs2-node-observability",
                "name": "fs2-dcgm-config-aaaaaaaaaaaaaaaa",
                "data_sha256": hashlib.sha256(canonical({"config.yaml": "collectors: []\n"})).hexdigest(),
            },
            {
                "component": "dcgm-metrics-config",
                "namespace": "fs2-node-observability",
                "name": "fs2-dcgm-metrics-bbbbbbbbbbbbbbbb",
                "data_sha256": hashlib.sha256(canonical({"metrics": "DCGM_FI_DEV_GPU_UTIL, gauge\n"})).hexdigest(),
            },
            {
                "component": "otel-node-config",
                "namespace": "fs2-node-observability",
                "name": "fs2-otel-node-relay-cccccccccccccccc",
                "data_sha256": hashlib.sha256(canonical({"relay": "receivers: {}\n"})).hexdigest(),
            },
        ],
        "pvc": {
            "namespace": "fs2-reference-data",
            "name": "fs2-reference-data-rwx",
            "uid": "pvc-test-uid",
            "resource_version": "1001",
            "volume_name": "pv-reference-data-test",
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
        "storage_evidence": {
            "read_proof_schema": "fs2-serve.nebius.ai/reference-data-csi-readiness/v2",
            "checkpoint_proof_schema": "fs2-serve.nebius.ai/checkpoint-durability-proof/v2",
            "probe_image": probe_image,
            "tools_config_map": "fs2-reference-data-tools-test",
            "tools_data_sha256": hashlib.sha256(canonical({"verify": "content"})).hexdigest(),
        },
        "successor_storage_sha256": hashlib.sha256(
            verifier._terraform_canonical(successor_storage)
        ).hexdigest(),
        "successor_storage": successor_storage,
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


def predecessor_live_object(item: dict[str, str]) -> dict[str, object]:
    value: dict[str, object] = {
        "apiVersion": item["api_version"],
        "kind": item["kind"],
        "metadata": {
            "name": item["name"],
            "namespace": item["namespace"],
            "uid": item["uid"],
            "resourceVersion": item["resource_version"],
            "generation": 1,
            "labels": {"app.kubernetes.io/managed-by": "terraform"},
            "annotations": {},
            "ownerReferences": [],
        },
    }
    if item["kind"] == "ConfigMap":
        value["immutable"] = True
        value["data"] = {"retained-predecessor": "true"}
    else:
        value["spec"] = {
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "retained-predecessor",
                            "image": "example.invalid/retained@sha256:" + "a" * 64,
                        }
                    ],
                }
            }
        }
        value["status"] = {"succeeded": 1}
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


def read_probe_objects(
    context: dict[str, object],
    claim: dict[str, object],
    namespace: str,
    claim_name: str,
    prefix: str,
    ordinal: int,
    generation_id: str | None = None,
) -> list[dict[str, object]]:
    dataset = context["dataset"]
    evidence = context["storage_evidence"]
    assert isinstance(dataset, dict) and isinstance(evidence, dict)
    generation: dict[str, object] | None = None
    if generation_id is not None:
        storage = context["successor_storage"]
        assert isinstance(storage, dict)
        ledger = storage["proof_generation_ledger"]
        assert isinstance(ledger, dict)
        generations = ledger["generations"]
        assert isinstance(generations, dict)
        generation = generations[generation_id]
        assert isinstance(generation, dict)
        dataset = {
            "id": generation["dataset_id"],
            "revision": generation["dataset_revision"],
            "tree_sha256": generation["dataset_tree_sha256"],
        }
        evidence = {
            "probe_image": generation["probe_image"],
            "tools_config_map": verifier._successor_tools_name(generation),
            "read_proof_schema": "fs2-serve.nebius.ai/reference-data-csi-readiness/v3",
        }
    claim_metadata = claim["metadata"]
    claim_spec = claim["spec"]
    assert isinstance(claim_metadata, dict) and isinstance(claim_spec, dict)
    receipt = f"receipts/{dataset['id']}/{dataset['revision']}.json"
    annotations = {
        "reference-data.fs2.nebius.ai/tree-sha256": dataset["tree_sha256"],
        "reference-data.fs2.nebius.ai/receipt": receipt,
        "reference-data.fs2.nebius.ai/pvc-uid": claim_metadata["uid"],
        "reference-data.fs2.nebius.ai/pvc-resource-version": claim_metadata["resourceVersion"],
        "reference-data.fs2.nebius.ai/volume-name": claim_spec["volumeName"],
        "reference-data.fs2.nebius.ai/proof-challenge": (
            generation["deployment_nonce"] if generation is not None else context["deployment_nonce"]
        ),
        "security.fs2.nebius.ai/verified-tree-sha256": dataset["tree_sha256"],
    }
    if generation is not None:
        annotations.update(
            {
                "security.fs2.nebius.ai/proof-generation": generation_id,
                "security.fs2.nebius.ai/proof-attempt": str(generation["attempt"]),
            }
        )
    command = [
        "python",
        "/opt/fs2/reference-data/verify_csi_readiness.py",
        "--root",
        "/reference-data",
        "--receipt",
        receipt,
        "--bundle",
        dataset["id"],
        "--revision",
        dataset["revision"],
        "--tree-sha256",
        dataset["tree_sha256"],
        "--pvc-uid",
        claim_metadata["uid"],
        "--pvc-resource-version",
        claim_metadata["resourceVersion"],
        "--volume-name",
        claim_spec["volumeName"],
        "--challenge",
        generation["deployment_nonce"] if generation is not None else context["deployment_nonce"],
    ]
    if generation is not None:
        command.extend(["--generation", generation_id, "--attempt", str(generation["attempt"])])
    command.extend(["--proof-output", "/dev/termination-log"])
    container = {
        "name": "read-probe",
        "image": evidence["probe_image"],
        "command": command,
        "terminationMessagePath": "/dev/termination-log",
        "terminationMessagePolicy": "File",
    }
    pod_spec = {
        "serviceAccountName": "fs2-reference-data",
        "automountServiceAccountToken": False,
        "containers": [container],
        "volumes": [
            {
                "name": "reference-data",
                "persistentVolumeClaim": {"claimName": claim_name, "readOnly": True},
            },
            {
                "name": "tools",
                "configMap": {"name": evidence["tools_config_map"]},
            },
        ],
    }
    job_name = (
        verifier._successor_probe_name(prefix, generation_id)
        if generation_id is not None
        else verifier._probe_name(prefix, context)
    )
    job = live_object("batch/v1", "Job", namespace, job_name, ordinal)
    job_metadata = job["metadata"]
    assert isinstance(job_metadata, dict)
    job_metadata["annotations"] = annotations
    job["spec"] = {
        "backoffLimit": 2 if generation is not None else 0,
        "activeDeadlineSeconds": 900,
        "completions": 1,
        "parallelism": 1,
        "manualSelector": False,
        "template": {"spec": pod_spec},
    }
    job["status"] = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
    proof: dict[str, object] = {
        "schema": evidence["read_proof_schema"],
        "bundle_id": dataset["id"],
        "revision": dataset["revision"],
        "tree_sha256": dataset["tree_sha256"],
        "receipt_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "read_probe_passed": True,
        "pvc": {
            "uid": claim_metadata["uid"],
            "resource_version": claim_metadata["resourceVersion"],
            "volume_name": claim_spec["volumeName"],
        },
        "challenge": generation["deployment_nonce"] if generation is not None else context["deployment_nonce"],
    }
    if generation is not None:
        proof["generation"] = generation_id
        proof["attempt"] = generation["attempt"]
    proof["proof_sha256"] = hashlib.sha256(canonical(proof)).hexdigest()
    pod = live_object("v1", "Pod", namespace, f"{job_name}-pod", ordinal + 1)
    pod_metadata = pod["metadata"]
    assert isinstance(pod_metadata, dict)
    pod_metadata["ownerReferences"] = [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job_metadata["name"],
            "uid": job_metadata["uid"],
            "controller": True,
        }
    ]
    pod["spec"] = pod_spec
    pod["status"] = {
        "containerStatuses": [
            {
                "name": "read-probe",
                "image": evidence["probe_image"],
                "imageID": "docker-pullable://" + str(evidence["probe_image"]),
                "state": {
                    "terminated": {
                        "exitCode": 0,
                        "message": json.dumps(proof, sort_keys=True, separators=(",", ":")),
                    }
                },
            }
        ]
    }
    return [job, pod]


def storage_tools(
    context: dict[str, object],
    namespace: str,
    ordinal: int,
    generation_id: str | None = None,
) -> dict[str, object]:
    if generation_id is None:
        evidence = context["storage_evidence"]
        assert isinstance(evidence, dict)
        name = str(evidence["tools_config_map"])
        data = {"verify": "content"}
    else:
        storage = context["successor_storage"]
        assert isinstance(storage, dict)
        ledger = storage["proof_generation_ledger"]
        assert isinstance(ledger, dict)
        generations = ledger["generations"]
        assert isinstance(generations, dict)
        generation = generations[generation_id]
        assert isinstance(generation, dict)
        name = verifier._successor_tools_name(generation)
        data = generation["tools_data"]
        assert isinstance(data, dict)
    config = live_object("v1", "ConfigMap", namespace, name, ordinal)
    config["immutable"] = True
    config["data"] = data
    return config


def checkpoint_probe_objects(
    context: dict[str, object],
    claim: dict[str, object],
    mode: str,
    ordinal: int,
) -> list[dict[str, object]]:
    storage = context["successor_storage"]
    assert isinstance(storage, dict)
    ledger = storage["proof_generation_ledger"]
    assert isinstance(ledger, dict)
    generation_id = ledger["active_generation"]
    generations = ledger["generations"]
    assert isinstance(generation_id, str) and isinstance(generations, dict)
    generation = generations[generation_id]
    assert isinstance(generation, dict)
    claim_metadata = claim["metadata"]
    claim_spec = claim["spec"]
    assert isinstance(claim_metadata, dict) and isinstance(claim_spec, dict)
    identity = {
        "uid": claim_metadata["uid"],
        "resource_version": claim_metadata["resourceVersion"],
        "volume_name": claim_spec["volumeName"],
    }
    command = [
        "python",
        "/opt/fs2/reference-data/verify_checkpoint_durability.py",
        mode,
        "--root",
        "/checkpoints",
        "--pvc-uid",
        identity["uid"],
        "--pvc-resource-version",
        identity["resource_version"],
        "--volume-name",
        identity["volume_name"],
        "--challenge",
        generation["deployment_nonce"],
        "--generation",
        generation_id,
        "--attempt",
        str(generation["attempt"]),
        "--proof-output",
        "/dev/termination-log",
    ]
    container = {
        "name": "durability-proof",
        "image": generation["probe_image"],
        "command": command,
        "terminationMessagePath": "/dev/termination-log",
        "terminationMessagePolicy": "File",
    }
    pod_spec = {
        "automountServiceAccountToken": False,
        "containers": [container],
        "volumes": [
            {
                "name": "checkpoints",
                "persistentVolumeClaim": {
                    "claimName": verifier.SNAPSHOT_CHECKPOINT_CLAIM[1],
                    "readOnly": mode == "read",
                },
            },
            {"name": "tools", "configMap": {"name": verifier._successor_tools_name(generation)}},
        ],
    }
    job = live_object(
        "batch/v1",
        "Job",
        verifier.SNAPSHOT_CHECKPOINT_CLAIM[0],
        verifier._checkpoint_probe_name(mode, context),
        ordinal,
    )
    job_metadata = job["metadata"]
    assert isinstance(job_metadata, dict)
    job_metadata["annotations"] = {
        "security.fs2.nebius.ai/pvc-uid": identity["uid"],
        "security.fs2.nebius.ai/pvc-resource-version": identity["resource_version"],
        "security.fs2.nebius.ai/volume-name": identity["volume_name"],
        "security.fs2.nebius.ai/proof-challenge": generation["deployment_nonce"],
        "security.fs2.nebius.ai/proof-generation": generation_id,
        "security.fs2.nebius.ai/proof-attempt": str(generation["attempt"]),
        "security.fs2.nebius.ai/proof-mode": mode,
    }
    job["spec"] = {
        "backoffLimit": 2,
        "activeDeadlineSeconds": 900,
        "completions": 1,
        "parallelism": 1,
        "manualSelector": False,
        "template": {"spec": pod_spec},
    }
    job["status"] = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
    marker = {
        "schema": "fs2-serve.nebius.ai/checkpoint-durability-marker/v2",
        "pvc": identity,
        "challenge": generation["deployment_nonce"],
        "generation": generation_id,
        "attempt": generation["attempt"],
    }
    proof: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/checkpoint-durability-proof/v2",
        "mode": mode,
        "pvc": identity,
        "challenge": generation["deployment_nonce"],
        "generation": generation_id,
        "attempt": generation["attempt"],
        "marker_sha256": hashlib.sha256(canonical(marker) + b"\n").hexdigest(),
    }
    proof["proof_sha256"] = hashlib.sha256(canonical(proof)).hexdigest()
    pod = live_object(
        "v1",
        "Pod",
        verifier.SNAPSHOT_CHECKPOINT_CLAIM[0],
        f"{verifier._checkpoint_probe_name(mode, context)}-pod",
        ordinal + 1,
    )
    pod_metadata = pod["metadata"]
    assert isinstance(pod_metadata, dict)
    pod_metadata["ownerReferences"] = [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job_metadata["name"],
            "uid": job_metadata["uid"],
            "controller": True,
        }
    ]
    pod["spec"] = pod_spec
    finished_at = NOW + dt.timedelta(seconds=1 if mode == "write" else 2)
    pod["status"] = {
        "containerStatuses": [
            {
                "name": "durability-proof",
                "image": generation["probe_image"],
                "imageID": "docker-pullable://" + str(generation["probe_image"]),
                "state": {
                    "terminated": {
                        "exitCode": 0,
                        "finishedAt": finished_at.isoformat().replace("+00:00", "Z"),
                        "message": json.dumps(proof, sort_keys=True, separators=(",", ":")),
                    }
                },
            }
        ]
    }
    return [job, pod]


def exception_objects() -> list[dict[str, object]]:
    identities = [
        ("v1", "Namespace", "", "fs2-node-observability"),
        *[("apps/v1", "DaemonSet", "fs2-node-observability", name) for name in sorted(verifier.EXCEPTION_DAEMONSETS)],
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
            "ValidatingAdmissionPolicy",
            "",
            "fs2-node-observability-configs",
        ),
        (
            "admissionregistration.k8s.io/v1",
            "ValidatingAdmissionPolicyBinding",
            "",
            "fs2-node-observability-configs",
        ),
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
        *[("v1", "ConfigMap", value["namespace"], value["name"]) for value in context_host_agent_configs()],
        (
            "admissionregistration.k8s.io/v1",
            "ValidatingAdmissionPolicyBinding",
            "",
            "fs2-node-observability-pods",
        ),
        *sorted(verifier.ROLLOUT_CUSTODY_OBJECTS),
        *sorted(verifier.SNAPSHOT_EXCEPTION_OBJECTS),
        *[
            ("apps/v1", "DaemonSet", identity[location]["namespace"], identity[location]["name"])
            for identity in context_host_agents()
            for location in ("legacy", "exception")
        ],
    ]
    identities = sorted(set(identities))
    objects = [
        live_object(api_version, kind, namespace, name, ordinal)
        for ordinal, (api_version, kind, namespace, name) in enumerate(identities, start=1)
    ]
    configs = {value["name"]: value for value in context_host_agent_configs()}
    config_data = {
        "dcgm-cold-config": {"config.yaml": "collectors: []\n"},
        "dcgm-metrics-config": {"metrics": "DCGM_FI_DEV_GPU_UTIL, gauge\n"},
        "otel-node-config": {"relay": "receivers: {}\n"},
    }
    for value in objects:
        if value["kind"] == "ConfigMap" and value["metadata"]["name"] in configs:  # type: ignore[index]
            config = configs[value["metadata"]["name"]]  # type: ignore[index]
            value["immutable"] = True
            value["data"] = config_data[config["component"]]
    return objects


def successor_storage_objects(context: dict[str, object], start: int = 20) -> list[dict[str, object]]:
    tree = context["dataset"]["tree_sha256"]  # type: ignore[index]
    custody = context["successor_storage"]
    assert isinstance(custody, dict)
    reference_source = custody["reference_source"]
    checkpoint_source = custody["checkpoint_source"]
    ledger = custody["proof_generation_ledger"]
    assert isinstance(reference_source, dict) and isinstance(checkpoint_source, dict) and isinstance(ledger, dict)
    generation_id = ledger["active_generation"]
    assert isinstance(generation_id, str)
    adoption = custody["predecessor_adoption"]
    assert isinstance(adoption, dict) and isinstance(adoption["resources"], list)
    values: list[dict[str, object]] = [
        predecessor_live_object(item)
        for item in adoption["resources"]
        if isinstance(item, dict)
    ]
    checkpoint_class = live_object(
        "storage.k8s.io/v1",
        "StorageClass",
        "",
        verifier.SNAPSHOT_CHECKPOINT_STORAGE_CLASS,
        start,
    )
    checkpoint_class["reclaimPolicy"] = "Retain"
    source_volume = live_object(
        "v1",
        "PersistentVolume",
        "",
        str(reference_source["persistent_volume_name"]),
        start + 1,
    )
    source_volume["metadata"]["uid"] = reference_source["uid"]  # type: ignore[index]
    source_volume["metadata"]["resourceVersion"] = reference_source["resource_version"]  # type: ignore[index]
    source_volume["spec"] = {
        "capacity": {"storage": reference_source["capacity_quantity"]},
        "accessModes": ["ReadWriteMany"],
        "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": verifier.REFERENCE_SUCCESSOR_STORAGE_CLASS,
        "csi": {
            "driver": reference_source["csi_driver"],
            "volumeHandle": reference_source["volume_handle"],
            "volumeAttributes": reference_source["volume_attributes"],
        },
    }
    values.extend([checkpoint_class, source_volume])
    ordinal = start + 2
    for namespace, name in verifier.REFERENCE_SUCCESSOR_CLAIMS:
        volume_name = verifier.REFERENCE_SUCCESSOR_VOLUMES[(namespace, name)]
        volume = live_object("v1", "PersistentVolume", "", volume_name, ordinal)
        volume["metadata"]["annotations"] = {  # type: ignore[index]
            "security.fs2.nebius.ai/custody-contract-sha256": context[
                "successor_storage_sha256"
            ],
            "security.fs2.nebius.ai/provisioning-receipt-sha256": reference_source[
                "provisioning_receipt_sha256"
            ],
            "security.fs2.nebius.ai/storage-owner": reference_source["storage_owner"],
        }
        volume["spec"] = {
            "capacity": {"storage": f"{reference_source['capacity_gib']}Gi"},
            "accessModes": ["ReadOnlyMany"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": verifier.REFERENCE_SUCCESSOR_STORAGE_CLASS,
            "claimRef": {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "name": name,
                "namespace": namespace,
            },
            "csi": {
                "driver": reference_source["csi_driver"],
                "volumeHandle": reference_source["volume_handle"],
                "volumeAttributes": reference_source["volume_attributes"],
                "readOnly": True,
            },
        }
        tools = storage_tools(context, namespace, ordinal, generation_id)
        claim = live_object("v1", "PersistentVolumeClaim", namespace, name, ordinal + 1)
        claim["metadata"]["annotations"] = {  # type: ignore[index]
            "security.fs2.nebius.ai/content-tree-sha256": tree,
            "security.fs2.nebius.ai/custody-contract-sha256": context[
                "successor_storage_sha256"
            ],
        }
        claim["spec"] = {
            "accessModes": ["ReadOnlyMany"],
            "storageClassName": verifier.REFERENCE_SUCCESSOR_STORAGE_CLASS,
            "volumeName": volume_name,
            "resources": {"requests": {"storage": f"{reference_source['capacity_gib']}Gi"}},
        }
        claim["status"] = {"phase": "Bound"}
        values.extend(
            [
                tools,
                volume,
                claim,
                *read_probe_objects(
                    context,
                    claim,
                    namespace,
                    name,
                    f"{name}-read-probe",
                    ordinal + 2,
                    generation_id,
                ),
            ]
        )
        ordinal += 5

    checkpoint_namespace, checkpoint_name = verifier.SNAPSHOT_CHECKPOINT_CLAIM
    checkpoint_volume = live_object(
        "v1",
        "PersistentVolume",
        "",
        verifier.SNAPSHOT_CHECKPOINT_VOLUME,
        ordinal,
    )
    checkpoint_volume["metadata"]["annotations"] = {  # type: ignore[index]
        "security.fs2.nebius.ai/custody-contract-sha256": context[
            "successor_storage_sha256"
        ],
        "security.fs2.nebius.ai/provisioning-receipt-sha256": checkpoint_source[
            "provisioning_receipt_sha256"
        ],
        "security.fs2.nebius.ai/storage-owner": checkpoint_source["storage_owner"],
    }
    checkpoint_volume["spec"] = {
        "capacity": {"storage": f"{checkpoint_source['capacity_gib']}Gi"},
        "accessModes": ["ReadWriteMany"],
        "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": verifier.SNAPSHOT_CHECKPOINT_STORAGE_CLASS,
        "claimRef": {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "name": checkpoint_name,
            "namespace": checkpoint_namespace,
        },
        "csi": {
            "driver": checkpoint_source["csi_driver"],
            "volumeHandle": checkpoint_source["volume_handle"],
            "volumeAttributes": checkpoint_source["volume_attributes"],
            "readOnly": False,
        },
    }
    checkpoint = live_object(
        "v1",
        "PersistentVolumeClaim",
        checkpoint_namespace,
        checkpoint_name,
        ordinal + 1,
    )
    checkpoint["metadata"]["annotations"] = {  # type: ignore[index]
        "security.fs2.nebius.ai/custody-contract-sha256": context[
            "successor_storage_sha256"
        ],
    }
    checkpoint["spec"] = {
        "accessModes": ["ReadWriteMany"],
        "storageClassName": verifier.SNAPSHOT_CHECKPOINT_STORAGE_CLASS,
        "volumeName": verifier.SNAPSHOT_CHECKPOINT_VOLUME,
        "resources": {"requests": {"storage": f"{checkpoint_source['requested_gib']}Gi"}},
    }
    checkpoint["status"] = {"phase": "Bound"}
    values.extend(
        [
            checkpoint_volume,
            checkpoint,
            *checkpoint_probe_objects(context, checkpoint, "write", ordinal + 2),
            *checkpoint_probe_objects(context, checkpoint, "read", ordinal + 4),
        ]
    )
    return values


def context_host_agent_configs() -> list[dict[str, str]]:
    return [
        {
            "component": "dcgm-cold-config",
            "namespace": "fs2-node-observability",
            "name": "fs2-dcgm-config-aaaaaaaaaaaaaaaa",
        },
        {
            "component": "dcgm-metrics-config",
            "namespace": "fs2-node-observability",
            "name": "fs2-dcgm-metrics-bbbbbbbbbbbbbbbb",
        },
        {
            "component": "otel-node-config",
            "namespace": "fs2-node-observability",
            "name": "fs2-otel-node-relay-cccccccccccccccc",
        },
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

    def get_object(self, api_version: str, kind: str, namespace: str, name: str) -> dict[str, object] | None:
        if (api_version, kind, namespace, name) == (
            "v1",
            "ConfigMap",
            "fs2-system",
            "fs2-pod-security-rollout-ledger",
        ):
            return deepcopy(self.ledger)
        value = self.objects.get((api_version, kind, namespace, name))
        return deepcopy(value) if value is not None else None

    def list_collection(
        self,
        api_version: str,
        kind: str,
        namespace: str,
        label_selector: str,
        field_selector: str,
    ) -> dict[str, object]:
        assert not label_selector and not field_selector
        items = [
            deepcopy(value)
            for (item_api, item_kind, item_namespace, _), value in self.objects.items()
            if (item_api, item_kind, item_namespace) == (api_version, kind, namespace)
        ]
        return {
            "apiVersion": "v1",
            "kind": "List",
            "metadata": {"resourceVersion": "collection-rv"},
            "items": items,
        }

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
        "schema": "fs2-serve.nebius.ai/sai07-baseline-inventory/v4",
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
        "schema": artifact["schema"],
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
    ledger.update(
        {
            "sequence": 1,
            "state": "baseline-captured",
            "last_bundle_sha256": "1" * 64,
            "last_receipt_id": "receipt-baseline-captured-1",
            "last_nonce": "nonce-baseline-captured-1",
            "authorization": {
                "phase": "bootstrap-baseline",
                "bundle_sha256": "1" * 64,
                "nonce": "nonce-baseline-captured-1",
                "owner_acknowledged": True,
                "downstream_acknowledged": True,
            },
        }
    )
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
            "sequence": 2,
            "from_state": "baseline-captured",
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
    storage = context["successor_storage"]
    assert isinstance(storage, dict)
    adoption = storage["predecessor_adoption"]
    assert isinstance(adoption, dict) and isinstance(adoption["resources"], list)
    objects = [
        *exception_objects(),
        *(
            predecessor_live_object(item)
            for item in adoption["resources"]
            if isinstance(item, dict)
        ),
    ]
    ledger = initial_ledger(query_value)
    path = signed_bundle(tmp_path, private_key, query_value, ledger, objects)
    return query_value, FakeClient(objects, ledger), path, objects


def test_receipt_context_rejects_legacy_v3_baseline(context: dict[str, object]) -> None:
    context["baseline"] = {
        "schema": "fs2-serve.nebius.ai/sai07-baseline-inventory/v3",
        "artifact_sha256": "a" * 64,
        "inventory_sha256": "b" * 64,
        "reference_host_paths": 103,
        "baseline_incompatible_objects": 103,
        "restricted_incompatible_objects": 716,
    }
    with pytest.raises(verifier.ReceiptError, match="baseline.schema is unsupported"):
        verifier._validate_context(context, deepcopy(context))


def test_receipt_context_rejects_more_than_eight_proof_generations(
    context: dict[str, object],
) -> None:
    context["baseline"] = {
        "schema": "fs2-serve.nebius.ai/sai07-baseline-inventory/v4",
        "artifact_sha256": "a" * 64,
        "inventory_sha256": "b" * 64,
        "reference_host_paths": 80,
        "baseline_incompatible_objects": 91,
        "restricted_incompatible_objects": 151,
    }
    storage = context["successor_storage"]
    assert isinstance(storage, dict)
    ledger = storage["proof_generation_ledger"]
    assert isinstance(ledger, dict)
    generations = ledger["generations"]
    assert isinstance(generations, dict)
    template = deepcopy(next(iter(generations.values())))
    assert isinstance(template, dict)
    for sequence in range(2, 10):
        generation = deepcopy(template)
        generation["sequence"] = sequence
        generation["attempt"] = sequence
        generation["deployment_nonce"] = f"retained-proof-{sequence}"
        generations[hashlib.sha256(verifier._terraform_canonical(generation)).hexdigest()] = generation
    context["successor_storage_sha256"] = hashlib.sha256(
        verifier._terraform_canonical(storage)
    ).hexdigest()
    with pytest.raises(verifier.ReceiptError, match="one through eight generations"):
        verifier._validate_context(context, deepcopy(context))


def test_durable_ledger_accepts_only_append_only_proof_generation_history(
    context: dict[str, object],
) -> None:
    ledger_query: dict[str, object] = {
        "expected_context": deepcopy(context),
        "expected_key_id": "sai07-review-authority",
        "expected_signer_identity": "platform-security-reviewer",
        "public_key_sha256": "e" * 64,
        "ledger_namespace": "fs2-system",
        "ledger_name": "fs2-pod-security-rollout-ledger",
    }
    durable = verifier._initial_ledger(ledger_query)
    config_map: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ledger_query["ledger_name"],
            "namespace": ledger_query["ledger_namespace"],
            "resourceVersion": "10",
        },
        "data": verifier._ledger_data(durable),
    }

    next_context = deepcopy(context)
    storage = next_context["successor_storage"]
    assert isinstance(storage, dict)
    generation_ledger = storage["proof_generation_ledger"]
    assert isinstance(generation_ledger, dict)
    generations = generation_ledger["generations"]
    assert isinstance(generations, dict)
    successor = deepcopy(next(iter(generations.values())))
    assert isinstance(successor, dict)
    successor["sequence"] = 2
    successor["attempt"] = 2
    successor["deployment_nonce"] = "sai07-proof-retry-2"
    successor_id = hashlib.sha256(verifier._terraform_canonical(successor)).hexdigest()
    generations[successor_id] = successor
    generation_ledger["active_generation"] = successor_id
    next_context["successor_storage_sha256"] = hashlib.sha256(
        verifier._terraform_canonical(storage)
    ).hexdigest()
    next_query = {**ledger_query, "expected_context": next_context}
    observed = verifier._ledger_from_config_map(config_map, next_query)
    assert observed["proof_generations"] == durable["proof_generations"]

    rewritten_context = deepcopy(next_context)
    rewritten_storage = rewritten_context["successor_storage"]
    assert isinstance(rewritten_storage, dict)
    rewritten_ledger = rewritten_storage["proof_generation_ledger"]
    assert isinstance(rewritten_ledger, dict)
    rewritten_generations = rewritten_ledger["generations"]
    assert isinstance(rewritten_generations, dict)
    first_id = next(iter(rewritten_generations))
    first = rewritten_generations.pop(first_id)
    assert isinstance(first, dict)
    first["deployment_nonce"] = "rewritten-first-generation"
    rewritten_generations[hashlib.sha256(verifier._terraform_canonical(first)).hexdigest()] = first
    rewritten_context["successor_storage_sha256"] = hashlib.sha256(
        verifier._terraform_canonical(rewritten_storage)
    ).hexdigest()
    with pytest.raises(verifier.ReceiptError, match="does not append"):
        verifier._ledger_from_config_map(
            config_map,
            {**ledger_query, "expected_context": rewritten_context},
        )


def test_exact_legacy_v2_ledger_is_adopted_in_memory_for_one_owner_transition(
    context: dict[str, object],
) -> None:
    legacy_data = {
        "schema": verifier.LEGACY_LEDGER_SCHEMA,
        "context_sha256": "1" * 64,
        "authority_key_id": "sai07-review-authority",
        "authority_signer_identity": "platform-security-reviewer",
        "authority_public_key_sha256": "e" * 64,
        "sequence": "1",
        "state": "baseline-captured",
        "last_bundle_sha256": "2" * 64,
        "last_receipt_id": "legacy-receipt",
        "last_nonce": "legacy-nonce",
        "authorization_phase": "bootstrap-baseline",
        "authorization_bundle_sha256": "2" * 64,
        "authorization_nonce": "legacy-nonce",
        "authorization_owner_acknowledged": "true",
        "authorization_downstream_acknowledged": "true",
    }
    storage = context["successor_storage"]
    assert isinstance(storage, dict)
    adoption = storage["predecessor_adoption"]
    assert isinstance(adoption, dict)
    adoption["rollout_ledger_v2_data_sha256"] = hashlib.sha256(
        verifier._terraform_canonical(legacy_data)
    ).hexdigest()
    context["successor_storage_sha256"] = hashlib.sha256(
        verifier._terraform_canonical(storage)
    ).hexdigest()
    query = {
        "expected_context": context,
        "expected_key_id": "sai07-review-authority",
        "expected_signer_identity": "platform-security-reviewer",
        "public_key_sha256": "e" * 64,
        "ledger_namespace": "fs2-system",
        "ledger_name": "fs2-pod-security-rollout-ledger",
    }
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "namespace": "fs2-system",
            "name": "fs2-pod-security-rollout-ledger",
            "resourceVersion": "44",
        },
        "data": legacy_data,
    }
    migrated = verifier._ledger_from_config_map(config_map, query)
    assert migrated["schema"] == verifier.LEDGER_SCHEMA
    assert migrated["sequence"] == 1
    assert migrated["state"] == "baseline-captured"
    assert migrated["proof_generations"] == verifier._proof_generation_state(context)

    tampered = deepcopy(config_map)
    tampered["data"]["last_nonce"] = "tampered"  # type: ignore[index]
    with pytest.raises(verifier.ReceiptError, match="differs from signed predecessor adoption"):
        verifier._ledger_from_config_map(tampered, query)


def test_fresh_v3_install_cannot_claim_predecessor_state(context: dict[str, object]) -> None:
    storage = context["successor_storage"]
    assert isinstance(storage, dict)
    adoption = storage["predecessor_adoption"]
    assert isinstance(adoption, dict)
    adoption.update(
        {
            "mode": "fresh-v3",
            "rollout_ledger_v2_data_sha256": None,
            "resources": [],
        }
    )
    context["successor_storage_sha256"] = hashlib.sha256(
        verifier._terraform_canonical(storage)
    ).hexdigest()
    verifier._validate_context(context, deepcopy(context))

    adoption["resources"] = [{"not": "empty"}]
    context["successor_storage_sha256"] = hashlib.sha256(
        verifier._terraform_canonical(storage)
    ).hexdigest()
    with pytest.raises(verifier.ReceiptError, match="may not claim predecessor"):
        verifier._validate_context(context, deepcopy(context))


def test_bootstrap_rejects_same_count_object_or_collection_drift(context: dict[str, object]) -> None:
    signed_context = deepcopy(context)
    signed_context["baseline"] = {
        "reference_host_paths": 80,
        "baseline_incompatible_objects": 91,
        "restricted_incompatible_objects": 151,
    }
    baseline = {
        "objects": [
            {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "namespace": "fs2-models",
                "name": "runtime-a",
                "uid": "uid-a",
                "resource_version": "10",
                "object_sha256": "a" * 64,
            }
        ],
        "collections": [
            {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "namespace": "fs2-models",
                "resource_version": "11",
                "item_count": 1,
            }
        ],
        "legacy_controller_objects": [],
    }
    live = {
        "live_inventory_objects": [
            {
                **baseline["objects"][0],
                "uid": "uid-b",
                "resource_version": "12",
                "object_sha256": "b" * 64,
            }
        ],
        "live_inventory_collections": [
            {
                **baseline["collections"][0],
                "resource_version": "13",
            }
        ],
        "live_reference_host_paths": 80,
        "live_baseline_incompatible_objects": 91,
        "live_restricted_incompatible_objects": 151,
        "live_legacy_controller_objects": [],
    }
    with pytest.raises(verifier.ReceiptError, match="immediate live inventory differs"):
        verifier._validate_baseline_live_inventory(baseline, live, signed_context)


def test_whole_bundle_live_verification_and_two_consumers_are_one_time(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    query_value, client, _, _ = setup_case(tmp_path, authority, context)
    owner = verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))
    assert owner["terminal_state"] == "exception-ready"

    resumed = verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=6))
    assert resumed["bundle_sha256"] == owner["bundle_sha256"]

    owner_ack_query = {**query_value, "mode": "owner-acknowledgement"}
    verifier.verify_and_consume(owner_ack_query, client, NOW + dt.timedelta(seconds=7))
    verifier.verify_and_consume(owner_ack_query, client, NOW + dt.timedelta(seconds=8))

    downstream_query = {**query_value, "mode": "downstream-authorization"}
    downstream = verifier.verify_and_consume(downstream_query, client, NOW + dt.timedelta(seconds=9))
    assert downstream["consumer"] == "downstream-authorization"
    verifier.verify_and_consume(downstream_query, client, NOW + dt.timedelta(seconds=10))
    downstream_ack_query = {**query_value, "mode": "downstream-acknowledgement"}
    verifier.verify_and_consume(downstream_ack_query, client, NOW + dt.timedelta(seconds=11))
    verifier.verify_and_consume(downstream_ack_query, client, NOW + dt.timedelta(seconds=12))


def test_exact_consumed_bundle_resumes_after_expiry_but_fresh_expired_bundle_is_rejected(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    query_value, client, _, _ = setup_case(tmp_path, authority, context)
    verifier.verify_and_consume(query_value, client, NOW + dt.timedelta(seconds=5))
    resumed = verifier.verify_and_consume(
        {**query_value, "mode": "owner-acknowledgement"},
        client,
        NOW + dt.timedelta(minutes=20),
    )
    assert resumed["terminal_state"] == "exception-ready"

    fresh_query, fresh_client, _, _ = setup_case(tmp_path, authority, context)
    with pytest.raises(verifier.ReceiptError, match="expired"):
        verifier.verify_and_consume(
            fresh_query,
            fresh_client,
            NOW + dt.timedelta(minutes=20),
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
    value["observations"]["observed_at"] = (NOW - dt.timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
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
    pvc["metadata"]["resourceVersion"] = context["pvc"]["resource_version"]  # type: ignore[index]
    pvc["spec"] = {
        "accessModes": ["ReadWriteMany"],
        "storageClassName": "fs2-reference-data-retained-sc",
        "volumeName": context["pvc"]["volume_name"],  # type: ignore[index]
        "resources": {"requests": {"storage": "1611Gi"}},
    }
    pvc["status"] = {"phase": "Bound"}
    storage_class = live_object("storage.k8s.io/v1", "StorageClass", "", "fs2-reference-data-retained-sc", 2)
    storage_class["reclaimPolicy"] = "Retain"
    probe, probe_pod = read_probe_objects(
        context,
        pvc,
        "fs2-reference-data",
        "fs2-reference-data-rwx",
        "fs2-reference-data-read-probe",
        3,
    )
    tools = storage_tools(context, "fs2-reference-data", 5)
    objects = [pvc, storage_class, tools, probe, probe_pod, *successor_storage_objects(context)]
    client = FakeClient(objects, {})
    verifier._validate_live_observations(
        client,
        [observation(value) for value in objects],
        [],
        "reference-data-ready",
        context,
    )

    probe["status"] = {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}
    successor_objects = successor_storage_objects(context)
    client = FakeClient([pvc, storage_class, tools, probe, probe_pod, *successor_objects], {})
    with pytest.raises(verifier.ReceiptError, match="read probe is not exactly completed"):
        verifier._validate_live_observations(
            client,
            [
                observation(pvc),
                observation(storage_class),
                observation(tools),
                observation(probe),
                observation(probe_pod),
                *(observation(value) for value in successor_objects),
            ],
            [],
            "reference-data-ready",
            context,
        )


def test_reference_data_transition_rejects_missing_bioir_and_snapshot_successors(
    context: dict[str, object],
) -> None:
    tree = context["dataset"]["tree_sha256"]  # type: ignore[index]
    pvc = live_object("v1", "PersistentVolumeClaim", "fs2-reference-data", "fs2-reference-data-rwx", 1)
    pvc["metadata"]["uid"] = context["pvc"]["uid"]  # type: ignore[index]
    pvc["metadata"]["resourceVersion"] = context["pvc"]["resource_version"]  # type: ignore[index]
    pvc["spec"] = {
        "accessModes": ["ReadWriteMany"],
        "storageClassName": "fs2-reference-data-retained-sc",
        "volumeName": context["pvc"]["volume_name"],  # type: ignore[index]
        "resources": {"requests": {"storage": "1611Gi"}},
    }
    pvc["status"] = {"phase": "Bound"}
    storage_class = live_object("storage.k8s.io/v1", "StorageClass", "", "fs2-reference-data-retained-sc", 2)
    storage_class["reclaimPolicy"] = "Retain"
    probe, probe_pod = read_probe_objects(
        context,
        pvc,
        "fs2-reference-data",
        "fs2-reference-data-rwx",
        "fs2-reference-data-read-probe",
        3,
    )
    tools = storage_tools(context, "fs2-reference-data", 5)
    objects = [pvc, storage_class, tools, probe, probe_pod]
    with pytest.raises(verifier.ReceiptError, match="successor storage classes are absent"):
        verifier._validate_live_observations(
            FakeClient(objects, {}),
            [observation(value) for value in objects],
            [],
            "reference-data-ready",
            context,
        )


def test_successor_storage_is_acknowledged_only_at_reference_data_ready(
    context: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def validate(
        client: object,
        expected_context: dict[str, object],
        *,
        include_successors: bool = False,
    ) -> None:
        assert client is not None
        assert expected_context is context
        calls.append(include_successors)

    monkeypatch.setattr(verifier, "_validate_reference_data_postcondition", validate)
    client = FakeClient(exception_objects(), {})
    verifier._validate_phase_acknowledgement(client, "migrate-reference-data", context, "downstream")
    verifier._validate_phase_acknowledgement(client, "cleanup-legacy-resources", context, "owner")
    verifier._validate_phase_acknowledgement(client, "cleanup-legacy-resources", context, "downstream")
    assert calls == [False, False, True]


def test_enforce_acknowledgement_requires_immediate_pinned_namespace_labels(
    context: dict[str, object],
) -> None:
    namespaces = ["fs2-data", "fs2-models", "fs2-observability", "fs2-system"]
    objects = [live_object("v1", "Namespace", "", name, index) for index, name in enumerate(namespaces, 1)]
    client = FakeClient(objects, {})
    with pytest.raises(verifier.ReceiptError, match="PSA enforcement acknowledgement differs"):
        verifier._validate_phase_acknowledgement(client, "enforce", context, "owner")

    for value in objects:
        value["metadata"]["labels"] = {  # type: ignore[index]
            "pod-security.kubernetes.io/enforce": "baseline",
            "pod-security.kubernetes.io/enforce-version": "v1.35",
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/audit-version": "v1.35",
            "pod-security.kubernetes.io/warn": "restricted",
            "pod-security.kubernetes.io/warn-version": "v1.35",
        }
    verifier._validate_phase_acknowledgement(FakeClient(objects, {}), "enforce", context, "owner")
