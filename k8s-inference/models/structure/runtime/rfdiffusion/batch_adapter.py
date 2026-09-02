#!/usr/bin/env python3
"""Typed RFdiffusion v1.1.0/Base-checkpoint scientific batch adapter."""

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


MODEL_ID = "rfdiffusion-upstream"
ADAPTER_ID = "rfdiffusion-upstream-v1-1-0-base"
SOURCE_REVISION = "9273ef67335acaf91df0150473a274759229cdf6"
OBSERVED_HEAD_REVISION = "86507b6538f51fce57b5a72477165f03999ed7ae"
CHECKPOINT_SHA256 = "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca"
ARTIFACT_CONTENT_SHA256 = "38617f06504291dd3f931bddfffc0932b879eec5ab1f2f1969facacdda2dd1f0"
PUBLIC_REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/rfdiffusion-upstream-base-parameters/v1"
ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
BATCH_NAMESPACE = NAMESPACE_BY_KIND["batch"]
BATCH_QUEUE = QUEUE_BY_NAMESPACE[BATCH_NAMESPACE]

DNS = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$")
CHAIN = re.compile(r"^[A-Za-z0-9]$")


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
            raise CatalogError("resolved RFdiffusion input manifest differs from its pointer")
    return manifest


def _pdb(payload: bytes) -> tuple[dict[tuple[str, int], dict[str, tuple[float, float, float]]], int, float]:
    residues: dict[tuple[str, int], dict[str, tuple[float, float, float]]] = {}
    coordinates: list[tuple[float, float, float]] = []
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise CatalogError("RFdiffusion PDB is not ASCII") from exc
    for line in lines:
        if not line.startswith("ATOM"):
            continue
        if len(line) < 54:
            raise CatalogError("RFdiffusion PDB contains a short ATOM record")
        try:
            residue_number = int(line[22:26])
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError as exc:
            raise CatalogError("RFdiffusion PDB has invalid residue or coordinate data") from exc
        if not all(math.isfinite(item) for item in xyz):
            raise CatalogError("RFdiffusion PDB has non-finite coordinates")
        key = (line[21:22].strip(), residue_number)
        atom = line[12:16].strip()
        if atom in residues.setdefault(key, {}):
            raise CatalogError("RFdiffusion PDB has duplicate atoms")
        residues[key][atom] = xyz
        coordinates.append(xyz)
    if not residues or any(not {"N", "CA", "C"}.issubset(atoms) for atoms in residues.values()):
        raise CatalogError("RFdiffusion PDB lacks a complete protein backbone")
    span = max(max(point[i] for point in coordinates) - min(point[i] for point in coordinates) for i in range(3))
    if span < 5.0:
        raise CatalogError("RFdiffusion PDB coordinates are degenerate")
    return residues, len(residues), span


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


def _input_structure(manifest: Mapping[str, Any], loader: Callable[[str], bytes], required: bool) -> dict[tuple[str, int], dict[str, tuple[float, float, float]]] | None:
    entries = manifest["entries"]
    if required:
        if len(entries) != 1 or entries[0]["name"] != "target_structure" or entries[0]["semantic_type"] != "protein-structure-pdb/v1":
            raise CatalogError("RFdiffusion motif/hotspot runs require target_structure:protein-structure-pdb/v1")
        return _pdb(_load(entries[0]["artifact"], loader, "RFdiffusion target structure"))[0]
    if len(entries) != 1 or entries[0]["name"] != "design_context" or entries[0]["semantic_type"] != "rfdiffusion-design-context-json/v1":
        raise CatalogError("unconditional RFdiffusion requires design_context:rfdiffusion-design-context-json/v1")
    try:
        context = json.loads(_load(entries[0]["artifact"], loader, "RFdiffusion design context"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError("RFdiffusion design context is not UTF-8 JSON") from exc
    if context != {"mode": "unconditional"}:
        raise CatalogError("RFdiffusion design context must select only unconditional mode")
    return None


def validate_request(value: Any, input_manifest_value: Any, *, artifact_loader: Callable[[str], bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _exact(value, {"schema", "operation", "service_class", "input_manifest", "parameters"}, "scientific run request", {"client_context"})
    if request["schema"] != PUBLIC_REQUEST_SCHEMA or request["operation"] != "design-backbone":
        raise CatalogError("RFdiffusion requires scientific-run-request/v1 operation=design-backbone")
    if request["service_class"] not in {"presentation", "interactive", "customer-batch", "bulk-backfill"}:
        raise CatalogError("RFdiffusion service_class is invalid")
    _validate_client_context(request)
    pointer = _pointer(request["input_manifest"], "input_manifest")
    manifest = _manifest(input_manifest_value, pointer, "RFdiffusion input manifest")
    parameters = _exact(request["parameters"], {"schema", "shard_count", "base_seed", "contigs", "hotspots", "diffusion_steps", "motif_rmsd_max_a"}, "RFdiffusion parameters")
    if parameters["schema"] != PARAMETER_SCHEMA:
        raise CatalogError("RFdiffusion parameter schema selects a different backend")
    count = _integer(parameters["shard_count"], 1, 128, "shard_count")
    seed = _integer(parameters["base_seed"], 0, 2_147_483_647, "base_seed")
    if seed + count - 1 > 2_147_483_647:
        raise CatalogError("RFdiffusion deterministic seeds overflow int32")
    contigs = parameters["contigs"]
    if not isinstance(contigs, list) or not contigs or len(contigs) > 64:
        raise CatalogError("RFdiffusion contigs must contain 1-64 typed segments")
    generated = 0
    maximum_length = 0
    motif_keys: set[tuple[str, int]] = set()
    for index, raw in enumerate(contigs):
        if not isinstance(raw, dict):
            raise CatalogError(f"contigs[{index}] must be an object")
        if raw.get("kind") == "generated":
            item = _exact(raw, {"kind", "minimum_length", "maximum_length"}, f"contigs[{index}]")
            lower = _integer(item["minimum_length"], 1, 512, "generated minimum_length")
            upper = _integer(item["maximum_length"], 1, 512, "generated maximum_length")
            if lower > upper:
                raise CatalogError("generated contig minimum exceeds maximum")
            maximum_length += upper
            generated += 1
        elif raw.get("kind") == "motif":
            item = _exact(raw, {"kind", "chain", "start", "end"}, f"contigs[{index}]")
            if not isinstance(item["chain"], str) or CHAIN.fullmatch(item["chain"]) is None:
                raise CatalogError("motif chain must be one character")
            start = _integer(item["start"], 1, 9999, "motif start")
            end = _integer(item["end"], 1, 9999, "motif end")
            if start > end:
                raise CatalogError("motif start exceeds end")
            maximum_length += end - start + 1
            motif_keys.update((item["chain"], residue) for residue in range(start, end + 1))
        else:
            raise CatalogError("RFdiffusion contig kind must be generated or motif")
    if generated == 0 or maximum_length > 512:
        raise CatalogError("RFdiffusion requires generated residues and at most 512 total residues")
    hotspots = parameters["hotspots"]
    if not isinstance(hotspots, list) or len(hotspots) > 64:
        raise CatalogError("RFdiffusion hotspots must be a bounded array")
    hotspot_keys: list[tuple[str, int]] = []
    for index, raw in enumerate(hotspots):
        item = _exact(raw, {"chain", "residue"}, f"hotspots[{index}]")
        if not isinstance(item["chain"], str) or CHAIN.fullmatch(item["chain"]) is None:
            raise CatalogError("hotspot chain must be one character")
        hotspot_keys.append((item["chain"], _integer(item["residue"], 1, 9999, "hotspot residue")))
    if hotspot_keys != sorted(set(hotspot_keys)):
        raise CatalogError("RFdiffusion hotspots must be sorted and unique")
    source = _input_structure(manifest, artifact_loader, bool(motif_keys or hotspot_keys))
    if source is not None and not (motif_keys | set(hotspot_keys)).issubset(source):
        raise CatalogError("RFdiffusion motif or hotspot residue is absent from the target structure")
    _integer(parameters["diffusion_steps"], 10, 200, "diffusion_steps")
    _number(parameters["motif_rmsd_max_a"], 0.1, 5.0, "motif_rmsd_max_a")
    return json.loads(json.dumps(request)), json.loads(json.dumps(manifest))


def _image(reference: str) -> tuple[str, str]:
    if not isinstance(reference, str) or reference.count("@") != 1:
        raise CatalogError("RFdiffusion runtime image must be immutable")
    digest = reference.rsplit("@", 1)[1]
    strong_sha256(digest, "RFdiffusion runtime image digest", image=True)
    return reference, digest


def _label(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or DNS.fullmatch(value) is None:
        raise CatalogError(f"{label} must be a DNS label of at most 63 characters")
    return value


def _job(*, image: str, name: str, command: list[str], gpu: bool, labels: dict[str, str], annotations: dict[str, str], config_name: str) -> dict[str, Any]:
    resources: dict[str, dict[str, Any]] = {
        "requests": {"cpu": "8" if gpu else "1", "memory": "48Gi" if gpu else "2Gi"},
        "limits": {"cpu": "16" if gpu else "2", "memory": "64Gi" if gpu else "4Gi"},
    }
    if gpu:
        resources["requests"]["nvidia.com/gpu"] = 1
        resources["limits"]["nvidia.com/gpu"] = 1
    pod_spec = {
        "serviceAccountName": "fs2-batch", "automountServiceAccountToken": False,
        "enableServiceLinks": False, "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 180,
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
        "spec": {"suspend": True, "backoffLimit": 0, "activeDeadlineSeconds": 21_600, "ttlSecondsAfterFinished": 86_400, "template": {"metadata": {"labels": labels, "annotations": annotations}, "spec": pod_spec}},
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
        "fs2.nebius.ai/checkpoint-sha256": CHECKPOINT_SHA256,
        "fs2.nebius.ai/artifact-content-sha256": ARTIFACT_CONTENT_SHA256,
        "fs2.nebius.ai/request-sha256": digest,
    }
    config_name = f"fs2-run-{token}"
    nodes: list[dict[str, Any]] = []
    shard_ids: list[str] = []
    for index in range(request["parameters"]["shard_count"]):
        node_id = f"diffuse-{index:03d}"
        shard_ids.append(node_id)
        seed = request["parameters"]["base_seed"] + index
        command = [
            "/opt/fs2/bin/rfdiffusion-batch", "run-shard",
            "--request", "/var/run/fs2/request.json",
            "--input-manifest", "/var/run/fs2/input-manifest.json",
            "--checkpoint", "/opt/fs2/models/Base_ckpt.pt",
            "--checkpoint-sha256", CHECKPOINT_SHA256,
            "--shard-index", str(index), "--seed", str(seed),
            "--output", f"{root}/shards/{index:03d}",
        ]
        nodes.append({
            "id": node_id, "stage_id": "diffuse", "depends_on": [], "seed": seed,
            "job": _job(image=image, name=f"rfdiffusion-{token}-s{index:03d}", command=command, gpu=True, labels=labels, annotations=annotations, config_name=config_name),
        })
    aggregate = [
        "/opt/fs2/bin/rfdiffusion-batch", "aggregate",
        "--request", "/var/run/fs2/request.json",
        "--input-manifest", "/var/run/fs2/input-manifest.json",
        "--shards", f"{root}/shards", "--expected-shards", str(len(shard_ids)),
        "--staging-manifest", f"{root}/output-manifest.json.tmp",
        "--output-manifest", f"{root}/output-manifest.json", "--atomic-rename",
    ]
    nodes.append({
        "id": "aggregate", "stage_id": "aggregate", "depends_on": shard_ids, "seed": None,
        "job": _job(image=image, name=f"rfdiffusion-{token}-aggregate", command=aggregate, gpu=False, labels=labels, annotations=annotations, config_name=config_name),
    })
    return {
        "schema": "fs2-serve.nebius.ai/rfdiffusion-batch-plan/v1",
        "model_id": MODEL_ID, "backend_id": ADAPTER_ID,
        "operation_id": operation_id, "workload_id": workload_id, "attempt_id": attempt_id,
        "request_sha256": digest, "runtime_image_digest": image_digest,
        "checkpoint_sha256": CHECKPOINT_SHA256, "nodes": nodes,
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


def _motif_rmsd(request: Mapping[str, Any], source: Mapping[tuple[str, int], Mapping[str, tuple[float, float, float]]], output: Mapping[tuple[str, int], Mapping[str, tuple[float, float, float]]]) -> float | None:
    motifs = [item for item in request["parameters"]["contigs"] if item["kind"] == "motif"]
    if not motifs:
        return None
    squared: list[float] = []
    for motif in motifs:
        for residue in range(motif["start"], motif["end"] + 1):
            key = (motif["chain"], residue)
            if key not in source or key not in output:
                raise CatalogError("RFdiffusion output does not retain every requested motif residue")
            for atom in ("N", "CA", "C"):
                squared.append(sum((source[key][atom][axis] - output[key][atom][axis]) ** 2 for axis in range(3)))
    return math.sqrt(sum(squared) / len(squared))


def validate_output_manifest(
    request_value: Any,
    input_manifest_value: Any,
    output_manifest_value: Any,
    *,
    artifact_loader: Callable[[str], bytes],
    expected_runtime_image_digest: str,
) -> dict[str, Any]:
    request, input_manifest = validate_request(request_value, input_manifest_value, artifact_loader=artifact_loader)
    strong_sha256(expected_runtime_image_digest, "admitted RFdiffusion runtime digest", image=True)
    manifest = _manifest(output_manifest_value, None, "RFdiffusion output manifest")
    entries = _entry_map(manifest)
    count = request["parameters"]["shard_count"]
    required = {"aggregate"} | {f"shard-{index:03d}" for index in range(count)}
    if not required.issubset(entries):
        raise CatalogError("RFdiffusion output manifest lacks complete shard accounting")
    for index in range(count):
        entry = entries[f"shard-{index:03d}"]
        if entry["semantic_type"] != "rfdiffusion-shard-result-json/v1":
            raise CatalogError("RFdiffusion shard has the wrong semantic type")
        shard = _exact(_json_entry(entry, artifact_loader, f"RFdiffusion shard {index}"), {"backend_id", "source_revision", "checkpoint_sha256", "index", "seed", "status"}, f"RFdiffusion shard {index}")
        if shard != {
            "backend_id": ADAPTER_ID, "source_revision": SOURCE_REVISION,
            "checkpoint_sha256": CHECKPOINT_SHA256, "index": index,
            "seed": request["parameters"]["base_seed"] + index, "status": "succeeded",
        }:
            raise CatalogError("RFdiffusion shard identity, seed, or status is invalid")
    aggregate_entry = entries["aggregate"]
    if aggregate_entry["semantic_type"] != "rfdiffusion-aggregate-json/v1":
        raise CatalogError("RFdiffusion aggregate has the wrong semantic type")
    aggregate = _exact(
        _json_entry(aggregate_entry, artifact_loader, "RFdiffusion aggregate"),
        {"backend_id", "source_revision", "checkpoint_sha256", "request_sha256", "runtime_image_digest", "expected_shards", "succeeded_shards", "atomic_commit"},
        "RFdiffusion aggregate",
    )
    if aggregate != {
        "backend_id": ADAPTER_ID, "source_revision": SOURCE_REVISION,
        "checkpoint_sha256": CHECKPOINT_SHA256, "request_sha256": request_digest(request),
        "runtime_image_digest": expected_runtime_image_digest,
        "expected_shards": count, "succeeded_shards": count, "atomic_commit": True,
    }:
        raise CatalogError("RFdiffusion aggregate is incomplete or belongs to another execution")
    lower = sum(item["minimum_length"] if item["kind"] == "generated" else item["end"] - item["start"] + 1 for item in request["parameters"]["contigs"])
    upper = sum(item["maximum_length"] if item["kind"] == "generated" else item["end"] - item["start"] + 1 for item in request["parameters"]["contigs"])
    motifs = any(item["kind"] == "motif" for item in request["parameters"]["contigs"])
    source = _input_structure(input_manifest, artifact_loader, motifs or bool(request["parameters"]["hotspots"]))
    metric_names = sorted(name for name in entries if re.fullmatch(r"candidate-[0-9]{3}-metrics", name))
    if not metric_names:
        raise CatalogError("RFdiffusion output contains no backbones")
    structure_names = {name for name in entries if re.fullmatch(r"candidate-[0-9]{3}-structure", name)}
    expected_structures = {f"{name.removesuffix('-metrics')}-structure" for name in metric_names}
    if structure_names != expected_structures or set(entries) != required | set(metric_names) | structure_names:
        raise CatalogError("RFdiffusion output contains unpaired or unknown entries")
    seen: set[str] = set()
    for metric_name in metric_names:
        prefix = metric_name.removesuffix("-metrics")
        structure_name = f"{prefix}-structure"
        if structure_name not in entries:
            raise CatalogError("RFdiffusion candidate lacks a structure")
        metric_entry, structure_entry = entries[metric_name], entries[structure_name]
        if metric_entry["semantic_type"] != "rfdiffusion-backbone-metrics-json/v1" or structure_entry["semantic_type"] != "protein-structure-pdb/v1":
            raise CatalogError("RFdiffusion candidate entries have wrong semantic types")
        candidate = _exact(_json_entry(metric_entry, artifact_loader, f"RFdiffusion {prefix}"), {"candidate_id", "shard_index", "seed", "backbone_complete", "ca_count", "coordinate_span_a", "motif_rmsd_a"}, f"RFdiffusion {prefix}")
        if not isinstance(candidate["candidate_id"], str) or OPAQUE.fullmatch(candidate["candidate_id"]) is None or candidate["candidate_id"] in seen:
            raise CatalogError("RFdiffusion candidate IDs must be unique")
        seen.add(candidate["candidate_id"])
        shard_index = _integer(candidate["shard_index"], 0, count - 1, "candidate shard_index")
        if candidate["seed"] != request["parameters"]["base_seed"] + shard_index:
            raise CatalogError("RFdiffusion candidate seed differs from its shard")
        output, residue_count, span = _pdb(_load(structure_entry["artifact"], artifact_loader, f"RFdiffusion {prefix} structure"))
        if not lower <= residue_count <= upper or candidate["backbone_complete"] is not True or candidate["ca_count"] != residue_count:
            raise CatalogError("RFdiffusion backbone violates contig length or completeness")
        if abs(_number(candidate["coordinate_span_a"], 5.0, 1_000_000.0, "coordinate span") - span) > 0.01:
            raise CatalogError("RFdiffusion coordinate span disagrees with the PDB")
        measured = _motif_rmsd(request, source or {}, output)
        if measured is None:
            if candidate["motif_rmsd_a"] is not None:
                raise CatalogError("unconditional RFdiffusion must not claim motif RMSD")
        else:
            reported = _number(candidate["motif_rmsd_a"], 0.0, request["parameters"]["motif_rmsd_max_a"], "motif RMSD")
            if abs(reported - measured) > 0.01:
                raise CatalogError("RFdiffusion motif RMSD disagrees with the structures")
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
