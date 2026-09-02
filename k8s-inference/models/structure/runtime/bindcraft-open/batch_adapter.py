#!/usr/bin/env python3
"""Model-local FreeBindCraft adapter that explicitly forbids PyRosetta."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.kubernetes import NAMESPACE_BY_KIND, QUEUE_BY_NAMESPACE
from fs2_serve_catalog.loader import CatalogError, strong_sha256


MODEL_ID = "freebindcraft"
ADAPTER_ID = "freebindcraft-v1-0-5"
SOURCE_REVISION = "28c43fc48942eebd7918f504e9812c5c17bb3411"
PUBLIC_REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/bindcraft-open-freebindcraft-parameters/v1"
ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
SETTINGS_SHA256 = "4124733af9dff65fb23e6a5f52b2329fc0d7a4ce5c50b6df225422f77fe467d6"
FILTERS_SHA256 = "4faeae2ed4a78b82ff8f9c3c763985ff0f0b97ebb9e10072d5d572424bb73206"
BATCH_NAMESPACE = NAMESPACE_BY_KIND["batch"]
BATCH_QUEUE = QUEUE_BY_NAMESPACE[BATCH_NAMESPACE]

AA = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$")
DNS = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$")
CHAIN = re.compile(r"^[A-Za-z0-9]$")
_RESIDUES = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


def _exact(value: Any, required: set[str], label: str, optional: set[str] | None = None) -> dict[str, Any]:
    allowed = required | (optional or set())
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - allowed:
        raise CatalogError(f"{label} has missing or unsupported fields")
    return value


def _integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CatalogError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _number(value: Any, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise CatalogError(f"{label} must be finite and between {minimum} and {maximum}")
    return result


def _pointer(value: Any, label: str) -> dict[str, Any]:
    item = _exact(value, {"artifact_id", "sha256", "size_bytes", "media_type"}, label, {"compression"})
    if not isinstance(item["artifact_id"], str) or OPAQUE.fullmatch(item["artifact_id"]) is None:
        raise CatalogError(f"{label}.artifact_id is invalid")
    strong_sha256(item["sha256"], f"{label}.sha256")
    _integer(item["size_bytes"], 0, 1 << 40, f"{label}.size_bytes")
    if not isinstance(item["media_type"], str) or MEDIA_TYPE.fullmatch(item["media_type"]) is None:
        raise CatalogError(f"{label}.media_type is invalid")
    if item.get("compression", "none") not in {"none", "gzip", "zstd"}:
        raise CatalogError(f"{label}.compression is invalid")
    return item


def _load(pointer: Mapping[str, Any], loader: Callable[[str], bytes], label: str) -> bytes:
    try:
        payload = loader(str(pointer["artifact_id"]))
    except (KeyError, OSError) as exc:
        raise CatalogError(f"{label} could not be resolved") from exc
    if not isinstance(payload, bytes) or len(payload) != pointer["size_bytes"] or hashlib.sha256(payload).hexdigest() != pointer["sha256"]:
        raise CatalogError(f"{label} differs from its content-addressed pointer")
    return payload


def _manifest(value: Any, pointer: Mapping[str, Any] | None, label: str) -> dict[str, Any]:
    manifest = _exact(value, {"schema", "manifest_id", "entries"}, label)
    if manifest["schema"] != ARTIFACT_MANIFEST_SCHEMA or not isinstance(manifest["manifest_id"], str) or OPAQUE.fullmatch(manifest["manifest_id"]) is None:
        raise CatalogError(f"{label} identity is invalid")
    if not isinstance(manifest["entries"], list) or not 1 <= len(manifest["entries"]) <= 10_000:
        raise CatalogError(f"{label}.entries must be a nonempty bounded array")
    names: set[str] = set()
    for index, raw in enumerate(manifest["entries"]):
        entry = _exact(raw, {"name", "semantic_type", "artifact"}, f"{label}.entries[{index}]")
        if not isinstance(entry["name"], str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", entry["name"]) or entry["name"] in names:
            raise CatalogError(f"{label} entry names must be unique and canonical")
        if not isinstance(entry["semantic_type"], str) or not re.fullmatch(r"[a-z][a-z0-9_.-]*/v[1-9][0-9]*", entry["semantic_type"]):
            raise CatalogError(f"{label} semantic type is invalid")
        names.add(entry["name"])
        _pointer(entry["artifact"], f"{label}.{entry['name']}")
    if pointer is not None:
        body = canonical_bytes(manifest)
        if pointer["artifact_id"] != manifest["manifest_id"] or pointer["media_type"] != "application/vnd.fs2.scientific-manifest+json" or len(body) != pointer["size_bytes"] or hashlib.sha256(body).hexdigest() != pointer["sha256"]:
            raise CatalogError("resolved input manifest differs from its request pointer")
    return manifest


def _pdb(payload: bytes) -> tuple[str, int, set[tuple[str, int]]]:
    residues: dict[tuple[str, str, str], dict[str, Any]] = {}
    coordinates: list[tuple[float, float, float]] = []
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise CatalogError("open BindCraft PDB is not ASCII") from exc
    for line in lines:
        if not line.startswith("ATOM"):
            continue
        if len(line) < 54:
            raise CatalogError("open BindCraft PDB has a short ATOM record")
        key = (line[21:22].strip(), line[22:26].strip(), line[26:27].strip())
        atom, residue = line[12:16].strip(), line[17:20].strip()
        try:
            number = int(line[22:26])
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError as exc:
            raise CatalogError("open BindCraft PDB has invalid coordinates") from exc
        if residue not in _RESIDUES or not all(math.isfinite(item) for item in xyz):
            raise CatalogError("open BindCraft PDB has unsupported residue data")
        item = residues.setdefault(key, {"name": residue, "atoms": set(), "number": number})
        if item["name"] != residue:
            raise CatalogError("open BindCraft PDB changes residue identity")
        item["atoms"].add(atom)
        coordinates.append(xyz)
    if not residues or any(not {"N", "CA", "C"}.issubset(item["atoms"]) for item in residues.values()):
        raise CatalogError("open BindCraft PDB lacks a complete backbone")
    span = max(max(point[i] for point in coordinates) - min(point[i] for point in coordinates) for i in range(3))
    if span < 5.0:
        raise CatalogError("open BindCraft PDB coordinates are degenerate")
    keys = {(chain, item["number"]) for (chain, _, _), item in residues.items()}
    return "".join(_RESIDUES[item["name"]] for item in residues.values()), len(residues), keys


def request_digest(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(request)).hexdigest()


def _validate_client_context(request: Mapping[str, Any]) -> None:
    if "client_context" not in request:
        return
    context = _exact(request["client_context"], set(), "client_context", {"batch_id", "correlation_id", "display_name"})
    for field in ("batch_id", "correlation_id"):
        if field in context and (not isinstance(context[field], str) or OPAQUE.fullmatch(context[field]) is None):
            raise CatalogError(f"client_context.{field} is invalid")
    if "display_name" in context and (not isinstance(context["display_name"], str) or not 1 <= len(context["display_name"]) <= 128):
        raise CatalogError("client_context.display_name is invalid")


def validate_request(value: Any, input_manifest_value: Any, *, artifact_loader: Callable[[str], bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _exact(value, {"schema", "operation", "service_class", "input_manifest", "parameters"}, "scientific run request", {"client_context"})
    if request["schema"] != PUBLIC_REQUEST_SCHEMA or request["operation"] != "design-binder":
        raise CatalogError("open BindCraft requires scientific-run-request/v1 operation=design-binder")
    if request["service_class"] not in {"presentation", "interactive", "customer-batch", "bulk-backfill"}:
        raise CatalogError("open BindCraft service_class is invalid")
    _validate_client_context(request)
    pointer = _pointer(request["input_manifest"], "input_manifest")
    manifest = _manifest(input_manifest_value, pointer, "open BindCraft input manifest")
    entries = manifest["entries"]
    if len(entries) != 1 or entries[0]["name"] != "target_structure" or entries[0]["semantic_type"] != "protein-structure-pdb/v1":
        raise CatalogError("open BindCraft requires exactly target_structure:protein-structure-pdb/v1")
    _, _, target_residues = _pdb(_load(entries[0]["artifact"], artifact_loader, "open BindCraft target"))
    parameters = _exact(request["parameters"], {"schema", "shard_count", "base_seed", "target_chains", "hotspots", "binder_length", "accepted_designs_per_shard", "max_trajectories_per_shard"}, "open BindCraft parameters")
    if parameters["schema"] != PARAMETER_SCHEMA:
        raise CatalogError("open BindCraft parameter schema selects a different backend")
    count = _integer(parameters["shard_count"], 1, 64, "shard_count")
    seed = _integer(parameters["base_seed"], 0, 2_147_483_647, "base_seed")
    if seed + count - 1 > 2_147_483_647:
        raise CatalogError("open BindCraft deterministic seeds overflow int32")
    chains = parameters["target_chains"]
    if not isinstance(chains, list) or not chains or chains != sorted(set(chains)) or any(not isinstance(item, str) or CHAIN.fullmatch(item) is None for item in chains):
        raise CatalogError("target_chains must be sorted, unique chain IDs")
    if not set(chains).issubset({chain for chain, _ in target_residues}):
        raise CatalogError("target_chains are absent from the target PDB")
    hotspots = parameters["hotspots"]
    if not isinstance(hotspots, list) or not hotspots or len(hotspots) > 64:
        raise CatalogError("hotspots must contain 1-64 typed residues")
    keys: list[tuple[str, int]] = []
    for index, raw in enumerate(hotspots):
        item = _exact(raw, {"chain", "residue"}, f"hotspots[{index}]")
        key = (item["chain"], _integer(item["residue"], 1, 9999, "hotspot residue"))
        if item["chain"] not in chains or key not in target_residues:
            raise CatalogError("hotspot is absent from selected target chains")
        keys.append(key)
    if keys != sorted(set(keys)):
        raise CatalogError("hotspots must be sorted and unique")
    lengths = _exact(parameters["binder_length"], {"minimum", "maximum"}, "binder_length")
    minimum = _integer(lengths["minimum"], 40, 200, "binder_length.minimum")
    maximum = _integer(lengths["maximum"], 40, 200, "binder_length.maximum")
    if minimum > maximum:
        raise CatalogError("binder length minimum exceeds maximum")
    _integer(parameters["accepted_designs_per_shard"], 1, 10, "accepted_designs_per_shard")
    _integer(parameters["max_trajectories_per_shard"], 1, 1000, "max_trajectories_per_shard")
    return json.loads(json.dumps(request)), json.loads(json.dumps(manifest))


def _image(reference: str) -> tuple[str, str]:
    if not isinstance(reference, str) or reference.count("@") != 1:
        raise CatalogError("open BindCraft runtime image must be immutable")
    digest = reference.rsplit("@", 1)[1]
    strong_sha256(digest, "open BindCraft runtime image digest", image=True)
    return reference, digest


def _label(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or DNS.fullmatch(value) is None:
        raise CatalogError(f"{label} must be a DNS label of at most 63 characters")
    return value


def _job(*, image: str, name: str, command: list[str], gpu: bool, labels: dict[str, str], annotations: dict[str, str], config_name: str) -> dict[str, Any]:
    resources: dict[str, dict[str, Any]] = {
        "requests": {"cpu": "16" if gpu else "2", "memory": "96Gi" if gpu else "4Gi"},
        "limits": {"cpu": "24" if gpu else "4", "memory": "128Gi" if gpu else "8Gi"},
    }
    if gpu:
        resources["requests"]["nvidia.com/gpu"] = 1
        resources["limits"]["nvidia.com/gpu"] = 1
    pod_spec = {
        "serviceAccountName": "fs2-batch", "automountServiceAccountToken": False,
        "enableServiceLinks": False, "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 300,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
        "imagePullSecrets": [{"name": "fs2-runtime-registry"}],
        "containers": [{
            "name": "batch", "image": image, "imagePullPolicy": "IfNotPresent",
            "command": command, "resources": resources,
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [
                {"name": "request", "mountPath": "/var/run/fs2", "readOnly": True},
                {"name": "workspace", "mountPath": "/workspace"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }],
        "volumes": [
            {"name": "request", "configMap": {"name": config_name}},
            {"name": "workspace", "persistentVolumeClaim": {"claimName": "fs2-cache"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "128Gi"}},
        ],
    }
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": BATCH_NAMESPACE, "labels": labels, "annotations": annotations},
        "spec": {"suspend": True, "backoffLimit": 0, "activeDeadlineSeconds": 86_400, "ttlSecondsAfterFinished": 86_400, "template": {"metadata": {"labels": labels, "annotations": annotations}, "spec": pod_spec}},
    }


def render_plan(
    value: Any,
    input_manifest_value: Any,
    *,
    artifact_loader: Callable[[str], bytes],
    runtime_image: str,
    operation_id: str,
    workload_id: str,
    attempt_id: str,
    tenant_id: str,
    local_queue: str = BATCH_QUEUE,
) -> dict[str, Any]:
    request, _ = validate_request(value, input_manifest_value, artifact_loader=artifact_loader)
    image, image_digest = _image(runtime_image)
    for item, label in ((workload_id, "workload_id"), (attempt_id, "attempt_id"), (tenant_id, "tenant_id"), (local_queue, "local_queue")):
        _label(item, label)
    if not isinstance(operation_id, str) or OPAQUE.fullmatch(operation_id) is None:
        raise CatalogError("operation_id must be a bounded opaque ID")
    digest = request_digest(request)
    token = hashlib.sha256(f"{tenant_id}:{operation_id}:{workload_id}:{attempt_id}".encode()).hexdigest()[:16]
    root = f"/workspace/runs/{token}"
    labels = {
        "app.kubernetes.io/name": "fs2-batch", "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-serve-models",
        "fs2.nebius.ai/model-id": MODEL_ID, "fs2.nebius.ai/workload-id": workload_id,
        "fs2.nebius.ai/attempt-id": attempt_id, "fs2.nebius.ai/tenant-id": tenant_id,
        "fs2.nebius.ai/service-class": request["service_class"],
        "fs2.nebius.ai/local-queue": local_queue,
        "kueue.x-k8s.io/queue-name": local_queue,
    }
    annotations = {
        "fs2.nebius.ai/operation-id": operation_id, "fs2.nebius.ai/backend-id": ADAPTER_ID,
        "fs2.nebius.ai/source-revision": SOURCE_REVISION,
        "fs2.nebius.ai/request-sha256": digest,
        "fs2.nebius.ai/access-profile": "standard",
        "fs2.nebius.ai/pyrosetta": "forbidden",
    }
    config_name = f"fs2-run-{token}"
    nodes: list[dict[str, Any]] = []
    shard_ids: list[str] = []
    for index in range(request["parameters"]["shard_count"]):
        node_id = f"trajectory-{index:03d}"
        shard_ids.append(node_id)
        seed = request["parameters"]["base_seed"] + index
        command = [
            "/opt/fs2/bin/freebindcraft-batch", "run-trajectory", "--backend-id", ADAPTER_ID,
            "--request", "/var/run/fs2/request.json",
            "--input-manifest", "/var/run/fs2/input-manifest.json",
            "--settings-template", "/opt/freebindcraft/settings_advanced/default_4stage_multimer.json",
            "--settings-sha256", SETTINGS_SHA256,
            "--filters", "/opt/freebindcraft/settings_filters/default_filters.json",
            "--filters-sha256", FILTERS_SHA256,
            "--shard-index", str(index), "--seed", str(seed),
            "--no-pyrosetta", "--pyrosetta-forbidden",
            "--output", f"{root}/shards/{index:03d}",
        ]
        nodes.append({
            "id": node_id, "stage_id": "trajectory", "depends_on": [], "seed": seed,
            "job": _job(image=image, name=f"freebindcraft-{token}-s{index:03d}", command=command, gpu=True, labels=labels, annotations=annotations, config_name=config_name),
        })
    aggregate = [
        "/opt/fs2/bin/freebindcraft-batch", "aggregate", "--backend-id", ADAPTER_ID,
        "--request", "/var/run/fs2/request.json",
        "--input-manifest", "/var/run/fs2/input-manifest.json",
        "--shards", f"{root}/shards", "--expected-shards", str(len(shard_ids)),
        "--staging-manifest", f"{root}/output-manifest.json.tmp",
        "--output-manifest", f"{root}/output-manifest.json", "--atomic-rename",
    ]
    nodes.append({
        "id": "aggregate", "stage_id": "aggregate", "depends_on": shard_ids, "seed": None,
        "job": _job(image=image, name=f"freebindcraft-{token}-aggregate", command=aggregate, gpu=False, labels=labels, annotations=annotations, config_name=config_name),
    })
    return {
        "schema": "fs2-serve.nebius.ai/bindcraft-open-batch-plan/v1",
        "model_id": MODEL_ID, "backend_id": ADAPTER_ID,
        "operation_id": operation_id, "workload_id": workload_id, "attempt_id": attempt_id,
        "request_sha256": digest, "runtime_image_digest": image_digest,
        "access_profile": "standard", "pyrosetta": "forbidden", "nodes": nodes,
    }


def _entry_map(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {entry["name"]: entry for entry in manifest["entries"]}


def _json_entry(entry: Mapping[str, Any], loader: Callable[[str], bytes], label: str) -> dict[str, Any]:
    try:
        value = json.loads(_load(entry["artifact"], loader, label))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{label} is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be a JSON object")
    return value


def validate_output_manifest(
    request_value: Any,
    input_manifest_value: Any,
    output_manifest_value: Any,
    *,
    artifact_loader: Callable[[str], bytes],
    expected_runtime_image_digest: str,
) -> dict[str, Any]:
    request, _ = validate_request(request_value, input_manifest_value, artifact_loader=artifact_loader)
    strong_sha256(expected_runtime_image_digest, "admitted open BindCraft runtime digest", image=True)
    manifest = _manifest(output_manifest_value, None, "open BindCraft output manifest")
    entries = _entry_map(manifest)
    count = request["parameters"]["shard_count"]
    required = {"aggregate"} | {f"shard-{index:03d}" for index in range(count)}
    if not required.issubset(entries):
        raise CatalogError("open BindCraft output manifest lacks complete shard accounting")
    for index in range(count):
        entry = entries[f"shard-{index:03d}"]
        if entry["semantic_type"] != "bindcraft-open-shard-result-json/v1":
            raise CatalogError("open BindCraft shard has the wrong semantic type")
        shard = _exact(_json_entry(entry, artifact_loader, f"open BindCraft shard {index}"), {"backend_id", "source_revision", "index", "seed", "status"}, f"open BindCraft shard {index}")
        if shard != {"backend_id": ADAPTER_ID, "source_revision": SOURCE_REVISION, "index": index, "seed": request["parameters"]["base_seed"] + index, "status": "succeeded"}:
            raise CatalogError("open BindCraft shard identity, seed, or status is invalid")
    aggregate_entry = entries["aggregate"]
    if aggregate_entry["semantic_type"] != "bindcraft-open-aggregate-json/v1":
        raise CatalogError("open BindCraft aggregate has the wrong semantic type")
    aggregate = _exact(
        _json_entry(aggregate_entry, artifact_loader, "open BindCraft aggregate"),
        {"backend_id", "source_revision", "access_profile", "pyrosetta", "request_sha256", "runtime_image_digest", "expected_shards", "succeeded_shards", "atomic_commit"},
        "open BindCraft aggregate",
    )
    if aggregate != {
        "backend_id": ADAPTER_ID, "source_revision": SOURCE_REVISION,
        "access_profile": "standard", "pyrosetta": "forbidden",
        "request_sha256": request_digest(request), "runtime_image_digest": expected_runtime_image_digest,
        "expected_shards": count, "succeeded_shards": count, "atomic_commit": True,
    }:
        raise CatalogError("open BindCraft aggregate is incomplete or belongs to another execution")
    metric_names = sorted(name for name in entries if re.fullmatch(r"candidate-[0-9]{3}-metrics", name))
    if not metric_names:
        raise CatalogError("open BindCraft output contains no accepted candidates")
    structure_names = {name for name in entries if re.fullmatch(r"candidate-[0-9]{3}-structure", name)}
    expected_structures = {f"{name.removesuffix('-metrics')}-structure" for name in metric_names}
    if structure_names != expected_structures or set(entries) != required | set(metric_names) | structure_names:
        raise CatalogError("open BindCraft output contains unpaired or unknown entries")
    lengths = request["parameters"]["binder_length"]
    seen: set[str] = set()
    for metric_name in metric_names:
        prefix = metric_name.removesuffix("-metrics")
        structure_name = f"{prefix}-structure"
        if structure_name not in entries:
            raise CatalogError("open BindCraft candidate lacks a structure")
        metric_entry, structure_entry = entries[metric_name], entries[structure_name]
        if metric_entry["semantic_type"] != "bindcraft-open-design-metrics-json/v1" or structure_entry["semantic_type"] != "protein-structure-pdb/v1":
            raise CatalogError("open BindCraft candidate entries have wrong semantic types")
        candidate = _exact(
            _json_entry(metric_entry, artifact_loader, f"open BindCraft {prefix}"),
            {"candidate_id", "shard_index", "seed", "sequence", "scoring_engine", "iptm", "mean_plddt", "openmm_energy_kj_mol", "buried_sasa_a2", "shape_complementarity"},
            f"open BindCraft {prefix}",
        )
        if candidate["scoring_engine"] != "openmm-freesasa" or not isinstance(candidate["candidate_id"], str) or OPAQUE.fullmatch(candidate["candidate_id"]) is None or candidate["candidate_id"] in seen:
            raise CatalogError("open BindCraft candidate identity or scoring engine is invalid")
        seen.add(candidate["candidate_id"])
        shard_index = _integer(candidate["shard_index"], 0, count - 1, "candidate shard_index")
        if candidate["seed"] != request["parameters"]["base_seed"] + shard_index:
            raise CatalogError("open BindCraft candidate seed differs from its shard")
        sequence = candidate["sequence"]
        if not isinstance(sequence, str) or AA.fullmatch(sequence) is None or not lengths["minimum"] <= len(sequence) <= lengths["maximum"]:
            raise CatalogError("open BindCraft sequence violates requested length")
        pdb_sequence, residues, _ = _pdb(_load(structure_entry["artifact"], artifact_loader, f"open BindCraft {prefix} structure"))
        if pdb_sequence != sequence or residues != len(sequence):
            raise CatalogError("open BindCraft binder-only PDB differs from candidate sequence")
        _number(candidate["iptm"], 0.0, 1.0, "open BindCraft iPTM")
        _number(candidate["mean_plddt"], 0.0, 1.0, "open BindCraft mean pLDDT")
        _number(candidate["openmm_energy_kj_mol"], -1_000_000.0, 1_000_000.0, "open BindCraft OpenMM energy")
        _number(candidate["buried_sasa_a2"], 0.0, 1_000_000.0, "open BindCraft buried SASA")
        _number(candidate["shape_complementarity"], 0.0, 1.0, "open BindCraft shape complementarity")
    return {
        "validator_id": ADAPTER_ID, "status": "passed",
        "request_sha256": request_digest(request),
        "output_manifest_sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
        "candidate_count": len(metric_names), "shard_count": count,
        "qualification_effect": "none-offline-validation-only",
    }


def load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CatalogError("JSON document must be an object")
    return value
