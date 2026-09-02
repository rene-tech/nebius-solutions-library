#!/usr/bin/env python3
"""Model-local adapter for the pinned mosaic Boltz2/ProteinMPNN recipe."""

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


MODEL_ID = "mosaic"
ADAPTER_ID = "mosaic-boltz2-proteinmpnn-v1"
SOURCE_REVISION = "70fec525423f5f87156a1a957b4a4048f9f8e676"
RECIPE_SHA256 = "cbfc7a88e6e7c2255730218bbdeaf6fc272d721b6c792231429a923309a8e0fe"
BOLTZ2_ARTIFACT_MANIFEST_SHA256 = (
    "c3bd08af34762feae697f9b2a0780f0ced57649b2a9d6fa5562786eceafc5e9e"
)
PUBLIC_REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/mosaic-boltz2-proteinmpnn-parameters/v1"
ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
BATCH_NAMESPACE = NAMESPACE_BY_KIND["batch"]
BATCH_QUEUE = QUEUE_BY_NAMESPACE[BATCH_NAMESPACE]

AA = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$")
DNS = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$")
_RESIDUES = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


def _exact(
    value: Any,
    required: set[str],
    label: str,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    allowed = required | (optional or set())
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - allowed:
        raise CatalogError(
            f"{label} must contain required {sorted(required)} and only optional "
            f"{sorted(optional or set())}"
        )
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
    pointer = _exact(
        value,
        {"artifact_id", "sha256", "size_bytes", "media_type"},
        label,
        {"compression"},
    )
    if not isinstance(pointer["artifact_id"], str) or OPAQUE.fullmatch(pointer["artifact_id"]) is None:
        raise CatalogError(f"{label}.artifact_id is not a bounded opaque ID")
    strong_sha256(pointer["sha256"], f"{label}.sha256")
    _integer(pointer["size_bytes"], 0, 1 << 40, f"{label}.size_bytes")
    media = pointer["media_type"]
    if not isinstance(media, str) or len(media) > 128 or MEDIA_TYPE.fullmatch(media) is None:
        raise CatalogError(f"{label}.media_type is invalid")
    if pointer.get("compression", "none") not in {"none", "gzip", "zstd"}:
        raise CatalogError(f"{label}.compression is invalid")
    return pointer


def _load(pointer: Mapping[str, Any], loader: Callable[[str], bytes], label: str) -> bytes:
    try:
        payload = loader(str(pointer["artifact_id"]))
    except (KeyError, OSError) as exc:
        raise CatalogError(f"{label} could not be resolved") from exc
    if not isinstance(payload, bytes):
        raise CatalogError(f"{label} resolver did not return bytes")
    if len(payload) != pointer["size_bytes"] or hashlib.sha256(payload).hexdigest() != pointer["sha256"]:
        raise CatalogError(f"{label} bytes differ from the content-addressed pointer")
    return payload


def _manifest(
    value: Any,
    pointer: Mapping[str, Any] | None,
    label: str,
) -> dict[str, Any]:
    manifest = _exact(value, {"schema", "manifest_id", "entries"}, label)
    if manifest["schema"] != ARTIFACT_MANIFEST_SCHEMA:
        raise CatalogError(f"{label} uses a different artifact-manifest schema")
    if not isinstance(manifest["manifest_id"], str) or OPAQUE.fullmatch(manifest["manifest_id"]) is None:
        raise CatalogError(f"{label}.manifest_id is invalid")
    entries = manifest["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 10_000:
        raise CatalogError(f"{label}.entries must be a nonempty bounded array")
    names: set[str] = set()
    for index, raw in enumerate(entries):
        entry = _exact(raw, {"name", "semantic_type", "artifact"}, f"{label}.entries[{index}]")
        name = entry["name"]
        semantic = entry["semantic_type"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", name)
            or name in names
        ):
            raise CatalogError(f"{label} entry names must be unique and canonical")
        if not isinstance(semantic, str) or not re.fullmatch(
            r"[a-z][a-z0-9_.-]*/v[1-9][0-9]*", semantic
        ):
            raise CatalogError(f"{label} semantic type is invalid")
        names.add(name)
        _pointer(entry["artifact"], f"{label}.{name}")
    if pointer is not None:
        if pointer["artifact_id"] != manifest["manifest_id"]:
            raise CatalogError("input_manifest artifact ID differs from manifest_id")
        if pointer["media_type"] != "application/vnd.fs2.scientific-manifest+json":
            raise CatalogError("input_manifest pointer has the wrong media type")
        payload = canonical_bytes(manifest)
        if (
            len(payload) != pointer["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != pointer["sha256"]
        ):
            raise CatalogError("resolved input manifest differs from its request pointer")
    return manifest


def _target_sequence(manifest: Mapping[str, Any], loader: Callable[[str], bytes]) -> str:
    entries = manifest["entries"]
    if (
        len(entries) != 1
        or entries[0]["name"] != "target_sequence"
        or entries[0]["semantic_type"] != "protein-sequence-fasta/v1"
    ):
        raise CatalogError(
            "mosaic input manifest requires exactly target_sequence:protein-sequence-fasta/v1"
        )
    payload = _load(entries[0]["artifact"], loader, "mosaic target sequence")
    try:
        lines = [line.strip() for line in payload.decode("ascii").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise CatalogError("mosaic target FASTA is not ASCII") from exc
    if len(lines) < 2 or not lines[0].startswith(">") or any(
        line.startswith(">") for line in lines[1:]
    ):
        raise CatalogError("mosaic target must be a single-record FASTA")
    sequence = "".join(lines[1:]).upper()
    if not 20 <= len(sequence) <= 1200 or AA.fullmatch(sequence) is None:
        raise CatalogError("mosaic target FASTA must contain 20-1200 canonical amino acids")
    return sequence


def request_digest(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(request)).hexdigest()


def _validate_client_context(request: Mapping[str, Any]) -> None:
    if "client_context" not in request:
        return
    context = _exact(
        request["client_context"], set(), "client_context",
        {"batch_id", "correlation_id", "display_name"},
    )
    for field in ("batch_id", "correlation_id"):
        if field in context and (not isinstance(context[field], str) or OPAQUE.fullmatch(context[field]) is None):
            raise CatalogError(f"client_context.{field} is invalid")
    if "display_name" in context and (not isinstance(context["display_name"], str) or not 1 <= len(context["display_name"]) <= 128):
        raise CatalogError("client_context.display_name is invalid")


def validate_request(
    value: Any,
    input_manifest_value: Any,
    *,
    artifact_loader: Callable[[str], bytes],
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _exact(
        value,
        {"schema", "operation", "service_class", "input_manifest", "parameters"},
        "scientific run request",
        {"client_context"},
    )
    if request["schema"] != PUBLIC_REQUEST_SCHEMA or request["operation"] != "design-binder":
        raise CatalogError(
            "mosaic requires scientific-run-request/v1 operation=design-binder"
        )
    if request["service_class"] not in {
        "presentation", "interactive", "customer-batch", "bulk-backfill",
    }:
        raise CatalogError("mosaic service_class is outside the shared scheduling contract")
    _validate_client_context(request)
    manifest_pointer = _pointer(request["input_manifest"], "input_manifest")
    manifest = _manifest(input_manifest_value, manifest_pointer, "mosaic input manifest")
    sequence = _target_sequence(manifest, artifact_loader)

    parameters = _exact(
        request["parameters"],
        {"schema", "shard_count", "base_seed", "hotspots", "binder_length", "optimizer_steps"},
        "mosaic parameters",
    )
    if parameters["schema"] != PARAMETER_SCHEMA:
        raise CatalogError("mosaic parameter schema selects a different backend")
    count = _integer(parameters["shard_count"], 1, 64, "shard_count")
    seed = _integer(parameters["base_seed"], 0, 2_147_483_647, "base_seed")
    if seed + count - 1 > 2_147_483_647:
        raise CatalogError("mosaic deterministic shard seeds overflow int32")
    hotspots = parameters["hotspots"]
    if (
        not isinstance(hotspots, list)
        or not hotspots
        or hotspots != sorted(set(hotspots))
        or len(hotspots) > 32
    ):
        raise CatalogError("mosaic hotspots must be a sorted unique nonempty list")
    for hotspot in hotspots:
        _integer(hotspot, 1, len(sequence), "mosaic hotspot")
    _integer(parameters["binder_length"], 40, 200, "binder_length")
    _integer(parameters["optimizer_steps"], 20, 500, "optimizer_steps")
    return json.loads(json.dumps(request)), json.loads(json.dumps(manifest))


def _image(reference: str) -> tuple[str, str]:
    if not isinstance(reference, str) or reference.count("@") != 1:
        raise CatalogError("mosaic runtime image must be an immutable reference@digest")
    digest = reference.rsplit("@", 1)[1]
    strong_sha256(digest, "mosaic runtime image digest", image=True)
    return reference, digest


def _label(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or DNS.fullmatch(value) is None:
        raise CatalogError(f"{label} must be a DNS label of at most 63 characters")
    return value


def _job(
    *,
    image: str,
    name: str,
    command: list[str],
    gpu: bool,
    labels: dict[str, str],
    annotations: dict[str, str],
    config_name: str,
) -> dict[str, Any]:
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise CatalogError("mosaic Job command must be direct nonempty argv")
    resources: dict[str, dict[str, Any]] = {
        "requests": {"cpu": "8" if gpu else "1", "memory": "64Gi" if gpu else "2Gi"},
        "limits": {"cpu": "16" if gpu else "2", "memory": "128Gi" if gpu else "4Gi"},
    }
    if gpu:
        resources["requests"]["nvidia.com/gpu"] = 1
        resources["limits"]["nvidia.com/gpu"] = 1
    pod_spec = {
        "serviceAccountName": "fs2-batch",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 90,
        "securityContext": {
            "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
            "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"},
        },
        "imagePullSecrets": [{"name": "fs2-runtime-registry"}],
        "containers": [{
            "name": "batch", "image": image, "imagePullPolicy": "IfNotPresent",
            "command": command, "resources": resources,
            "securityContext": {
                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [
                {"name": "request", "mountPath": "/var/run/fs2", "readOnly": True},
                {"name": "workspace", "mountPath": "/workspace"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }],
        "volumes": [
            {"name": "request", "configMap": {"name": config_name}},
            {"name": "workspace", "persistentVolumeClaim": {"claimName": "fs2-cache"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "16Gi"}},
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name, "namespace": BATCH_NAMESPACE,
            "labels": labels, "annotations": annotations,
        },
        "spec": {
            "suspend": True,
            "backoffLimit": 0,
            "activeDeadlineSeconds": 43_200,
            "ttlSecondsAfterFinished": 86_400,
            "template": {
                "metadata": {"labels": labels, "annotations": annotations},
                "spec": pod_spec,
            },
        },
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
    request, _ = validate_request(
        value, input_manifest_value, artifact_loader=artifact_loader
    )
    image, image_digest = _image(runtime_image)
    for item, label in (
        (workload_id, "workload_id"), (attempt_id, "attempt_id"),
        (tenant_id, "tenant_id"), (local_queue, "local_queue"),
    ):
        _label(item, label)
    if not isinstance(operation_id, str) or OPAQUE.fullmatch(operation_id) is None:
        raise CatalogError("operation_id must be a bounded opaque ID")
    digest = request_digest(request)
    token = hashlib.sha256(
        f"{tenant_id}:{operation_id}:{workload_id}:{attempt_id}".encode()
    ).hexdigest()[:16]
    root = f"/workspace/runs/{token}"
    labels = {
        "app.kubernetes.io/name": "fs2-batch",
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "fs2-serve-models",
        "fs2.nebius.ai/model-id": MODEL_ID,
        "fs2.nebius.ai/workload-id": workload_id,
        "fs2.nebius.ai/attempt-id": attempt_id,
        "fs2.nebius.ai/tenant-id": tenant_id,
        "fs2.nebius.ai/service-class": request["service_class"],
        "fs2.nebius.ai/local-queue": local_queue,
        "kueue.x-k8s.io/queue-name": local_queue,
    }
    annotations = {
        "fs2.nebius.ai/operation-id": operation_id,
        "fs2.nebius.ai/backend-id": ADAPTER_ID,
        "fs2.nebius.ai/source-revision": SOURCE_REVISION,
        "fs2.nebius.ai/recipe-sha256": RECIPE_SHA256,
        "fs2.nebius.ai/request-sha256": digest,
    }
    config_name = f"fs2-run-{token}"
    nodes: list[dict[str, Any]] = []
    shard_ids: list[str] = []
    for index in range(request["parameters"]["shard_count"]):
        node_id = f"design-{index:03d}"
        shard_ids.append(node_id)
        seed = request["parameters"]["base_seed"] + index
        command = [
            "/opt/fs2/bin/mosaic-batch", "run-shard",
            "--request", "/var/run/fs2/request.json",
            "--input-manifest", "/var/run/fs2/input-manifest.json",
            "--recipe", "/opt/fs2/mosaic/recipe.json",
            "--recipe-sha256", RECIPE_SHA256,
            "--shard-index", str(index), "--seed", str(seed),
            "--output", f"{root}/shards/{index:03d}",
        ]
        nodes.append({
            "id": node_id,
            "stage_id": "design",
            "depends_on": [],
            "seed": seed,
            "job": _job(
                image=image, name=f"mosaic-{token}-s{index:03d}", command=command,
                gpu=True, labels=labels, annotations=annotations,
                config_name=config_name,
            ),
        })
    aggregate = [
        "/opt/fs2/bin/mosaic-batch", "aggregate",
        "--request", "/var/run/fs2/request.json",
        "--input-manifest", "/var/run/fs2/input-manifest.json",
        "--shards", f"{root}/shards",
        "--expected-shards", str(len(shard_ids)),
        "--staging-manifest", f"{root}/output-manifest.json.tmp",
        "--output-manifest", f"{root}/output-manifest.json",
        "--atomic-rename",
    ]
    nodes.append({
        "id": "aggregate",
        "stage_id": "aggregate",
        "depends_on": shard_ids,
        "seed": None,
        "job": _job(
            image=image, name=f"mosaic-{token}-aggregate", command=aggregate,
            gpu=False, labels=labels, annotations=annotations,
            config_name=config_name,
        ),
    })
    return {
        "schema": "fs2-serve.nebius.ai/mosaic-batch-plan/v1",
        "model_id": MODEL_ID,
        "backend_id": ADAPTER_ID,
        "operation_id": operation_id,
        "workload_id": workload_id,
        "attempt_id": attempt_id,
        "request_sha256": digest,
        "runtime_image_digest": image_digest,
        "recipe_sha256": RECIPE_SHA256,
        "nodes": nodes,
    }


def _pdb(payload: bytes) -> tuple[str, int]:
    residues: dict[tuple[str, str, str], dict[str, Any]] = {}
    coordinates: list[tuple[float, float, float]] = []
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise CatalogError("mosaic output PDB is not ASCII") from exc
    for raw in lines:
        if not raw.startswith("ATOM"):
            continue
        if len(raw) < 54:
            raise CatalogError("mosaic output PDB contains a short ATOM record")
        key = (raw[21:22].strip(), raw[22:26].strip(), raw[26:27].strip())
        atom, residue = raw[12:16].strip(), raw[17:20].strip()
        try:
            xyz = (float(raw[30:38]), float(raw[38:46]), float(raw[46:54]))
        except ValueError as exc:
            raise CatalogError("mosaic output PDB has invalid coordinates") from exc
        if not all(math.isfinite(item) for item in xyz) or residue not in _RESIDUES:
            raise CatalogError("mosaic output PDB has unsupported residue or coordinate data")
        entry = residues.setdefault(key, {"name": residue, "atoms": set()})
        if entry["name"] != residue:
            raise CatalogError("mosaic output PDB changes residue identity within a position")
        entry["atoms"].add(atom)
        coordinates.append(xyz)
    if not residues or any(
        not {"N", "CA", "C"}.issubset(item["atoms"]) for item in residues.values()
    ):
        raise CatalogError("mosaic output PDB lacks a complete protein backbone")
    extents = [
        max(point[i] for point in coordinates) - min(point[i] for point in coordinates)
        for i in range(3)
    ]
    if max(extents) < 5.0:
        raise CatalogError("mosaic output PDB coordinates are degenerate")
    return "".join(_RESIDUES[item["name"]] for item in residues.values()), len(residues)


def _entry_map(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {entry["name"]: entry for entry in manifest["entries"]}


def _json_entry(
    entry: Mapping[str, Any],
    loader: Callable[[str], bytes],
    label: str,
) -> dict[str, Any]:
    payload = _load(entry["artifact"], loader, label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{label} is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must contain a JSON object")
    return value


def validate_output_manifest(
    request_value: Any,
    input_manifest_value: Any,
    output_manifest_value: Any,
    *,
    artifact_loader: Callable[[str], bytes],
    expected_runtime_image_digest: str,
) -> dict[str, Any]:
    request, _ = validate_request(
        request_value, input_manifest_value, artifact_loader=artifact_loader
    )
    strong_sha256(expected_runtime_image_digest, "admitted mosaic runtime digest", image=True)
    manifest = _manifest(output_manifest_value, None, "mosaic output manifest")
    entries = _entry_map(manifest)
    count = request["parameters"]["shard_count"]
    required = {"aggregate"} | {f"shard-{index:03d}" for index in range(count)}
    missing = required - set(entries)
    if missing:
        raise CatalogError(f"mosaic output manifest lacks required entries: {sorted(missing)}")

    for index in range(count):
        entry = entries[f"shard-{index:03d}"]
        if entry["semantic_type"] != "mosaic-shard-result-json/v1":
            raise CatalogError("mosaic shard entry has the wrong semantic type")
        shard = _exact(
            _json_entry(entry, artifact_loader, f"mosaic shard {index}"),
            {"backend_id", "source_revision", "recipe_sha256", "index", "seed", "status"},
            f"mosaic shard {index}",
        )
        expected = {
            "backend_id": ADAPTER_ID,
            "source_revision": SOURCE_REVISION,
            "recipe_sha256": RECIPE_SHA256,
            "index": index,
            "seed": request["parameters"]["base_seed"] + index,
            "status": "succeeded",
        }
        if shard != expected:
            raise CatalogError("mosaic shard identity, seed, or status is invalid")

    aggregate_entry = entries["aggregate"]
    if aggregate_entry["semantic_type"] != "mosaic-aggregate-json/v1":
        raise CatalogError("mosaic aggregate entry has the wrong semantic type")
    aggregate = _exact(
        _json_entry(aggregate_entry, artifact_loader, "mosaic aggregate"),
        {
            "backend_id", "source_revision", "recipe_sha256", "request_sha256",
            "runtime_image_digest", "expected_shards", "succeeded_shards",
            "atomic_commit",
        },
        "mosaic aggregate",
    )
    if aggregate != {
        "backend_id": ADAPTER_ID,
        "source_revision": SOURCE_REVISION,
        "recipe_sha256": RECIPE_SHA256,
        "request_sha256": request_digest(request),
        "runtime_image_digest": expected_runtime_image_digest,
        "expected_shards": count,
        "succeeded_shards": count,
        "atomic_commit": True,
    }:
        raise CatalogError("mosaic aggregate is incomplete or bound to a different execution")

    metric_names = sorted(
        name for name in entries if re.fullmatch(r"candidate-[0-9]{3}-metrics", name)
    )
    if not metric_names:
        raise CatalogError("mosaic output manifest contains no candidates")
    structure_names = {
        name for name in entries if re.fullmatch(r"candidate-[0-9]{3}-structure", name)
    }
    expected_structures = {
        f"{name.removesuffix('-metrics')}-structure" for name in metric_names
    }
    if structure_names != expected_structures or set(entries) != required | set(metric_names) | structure_names:
        raise CatalogError("mosaic output manifest contains unpaired or unknown entries")
    seen: set[str] = set()
    for metric_name in metric_names:
        prefix = metric_name.removesuffix("-metrics")
        structure_name = f"{prefix}-structure"
        if structure_name not in entries:
            raise CatalogError(f"mosaic candidate {prefix} lacks its structure entry")
        metric_entry, structure_entry = entries[metric_name], entries[structure_name]
        if (
            metric_entry["semantic_type"] != "mosaic-design-metrics-json/v1"
            or structure_entry["semantic_type"] != "protein-structure-pdb/v1"
        ):
            raise CatalogError("mosaic candidate entries have the wrong semantic types")
        candidate = _exact(
            _json_entry(metric_entry, artifact_loader, f"mosaic candidate {prefix}"),
            {"candidate_id", "shard_index", "seed", "sequence", "iptm", "mean_plddt", "objective"},
            f"mosaic candidate {prefix}",
        )
        if not isinstance(candidate["candidate_id"], str) or OPAQUE.fullmatch(candidate["candidate_id"]) is None or candidate["candidate_id"] in seen:
            raise CatalogError("mosaic candidate IDs must be unique")
        seen.add(candidate["candidate_id"])
        shard_index = _integer(
            candidate["shard_index"], 0, count - 1, "mosaic candidate shard_index"
        )
        if candidate["seed"] != request["parameters"]["base_seed"] + shard_index:
            raise CatalogError("mosaic candidate seed does not match its deterministic shard")
        sequence = candidate["sequence"]
        if (
            not isinstance(sequence, str)
            or AA.fullmatch(sequence) is None
            or len(sequence) != request["parameters"]["binder_length"]
        ):
            raise CatalogError("mosaic candidate sequence violates the requested binder length")
        pdb_sequence, residues = _pdb(
            _load(
                structure_entry["artifact"],
                artifact_loader,
                f"mosaic candidate {prefix} structure",
            )
        )
        if pdb_sequence != sequence or residues != len(sequence):
            raise CatalogError("mosaic binder-only PDB sequence differs from candidate metrics")
        _number(candidate["iptm"], 0.0, 1.0, "mosaic iPTM")
        _number(candidate["mean_plddt"], 0.0, 1.0, "mosaic mean pLDDT")
        _number(candidate["objective"], -1_000_000.0, 1_000_000.0, "mosaic objective")
    return {
        "validator_id": ADAPTER_ID,
        "status": "passed",
        "request_sha256": request_digest(request),
        "output_manifest_sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
        "candidate_count": len(metric_names),
        "shard_count": count,
        "qualification_effect": "none-offline-validation-only",
    }


def load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CatalogError("JSON document must be an object")
    return value
