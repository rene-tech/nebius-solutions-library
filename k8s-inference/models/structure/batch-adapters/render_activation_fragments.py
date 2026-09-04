#!/usr/bin/env python3
"""Render model-owned activation fragments for the secondary and academic models.

Each of ESMFold2, ESMFold2-Fast, Protenix v2, OpenFold3 and AlphaFold 3 gets
three deterministic documents under ``<model>/activation/``:

* ``workload-profile.json`` -- a complete ``scientific-workload-profile/v1``
  entry wrapped in the same projection envelope the onboarding compiler emits,
  ready to be appended to ``catalog/runtime/contracts/scientific-workload-profiles.json``
  by the serialized integration step.  Every identity is derived from the
  adapter module, its model-owned contract, the published image handoff and
  the artifact catalog, so the fragment cannot drift from the code it describes.
* ``execution-map-fragment.json`` -- the matching ``scientific-execution-map/v3``
  model entry.  Identities that only a live localization can prove are left
  ``null`` and listed under ``activation_gates``; nothing here is invented.
* ``integration-recipe.json`` -- the exact shared edits the integration owner
  must make (allow-list, recipe paths, test pins), the recipe digest computed
  on this branch, and the blockers that still stand between the fragment and a
  live route.

The script never edits a shared aggregate.  ``--check`` fails when a committed
fragment differs from what the current code would render.

Run from ``k8s-inference``::

    PYTHONPATH=components/control-plane/src:catalog/runtime \\
      python3 models/structure/batch-adapters/render_activation_fragments.py [--check] [--model ID ...]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
CONTROL_PLANE_SRC = SOLUTION_ROOT / "components/control-plane/src"
CATALOG_RUNTIME = SOLUTION_ROOT / "catalog/runtime"
for candidate in (CONTROL_PLANE_SRC, CATALOG_RUNTIME):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

PROFILE_SCHEMA_ID = "fs2-serve.nebius.ai/scientific-workload-profile/v1"
PROFILE_PROJECTION_SCHEMA = "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1"
PROFILE_MERGE_TARGET = "catalog/runtime/contracts/scientific-workload-profiles.json"
EXECUTION_FRAGMENT_SCHEMA = "fs2-serve.nebius.ai/scientific-execution-map-fragment/v1"
EXECUTION_MAP_SCHEMA = "fs2-serve.nebius.ai/scientific-execution-map/v3"
EXECUTION_MERGE_TARGET = "catalog/runtime/contracts/scientific-execution-map.json"
RECIPE_SCHEMA = "fs2-serve.nebius.ai/scientific-activation-integration-recipe/v1"
REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/scientific-run-result/v1"
IMAGE_HANDOFF = ADAPTER_ROOT / "secondary-r4-image-handoff.json"
ARTIFACT_CATALOG = SOLUTION_ROOT / "model-artifacts"
AF3_TERMINAL_RECEIPT = SOLUTION_ROOT / "reference-data/evidence/af3-terminal-receipt-20260903.json"
AF3_STAGE_RECEIPT = SOLUTION_ROOT / "academic-assets/evidence/jobs/academic-assets-stage-v1.json"
REFERENCE_HOST_ROOT = "/mnt/fs2-reference-data/data"
REFERENCE_MOUNT_PATH = "/reference-data"
REFERENCE_STORAGE_LABEL = "storage.fs2.nebius/reference-data"
ACCELERATOR_CLASS_LABEL = "accelerator.fs2.nebius/class"
H100_CLASS = "nvidia-h100-sxm5-80gb"
H100_POOLS = ["h100-1x", "h100-reserved-8x"]
GIB = 1024**3

GPU_STAGE_RESOURCES = {
    "requests": {"cpu": "8000m", "memory": "96Gi", "ephemeral_storage": "32Gi"},
    "limits": {"cpu": "32000m", "memory": "256Gi", "ephemeral_storage": "128Gi"},
}
CPU_STAGE_RESOURCES = {
    "requests": {"cpu": "4000m", "memory": "16Gi", "ephemeral_storage": "32Gi"},
    "limits": {"cpu": "8000m", "memory": "32Gi", "ephemeral_storage": "64Gi"},
}
STAGE_DEADLINE_SECONDS = 43_200
STAGE_GRACE_SECONDS = 90

SHARED_GATES = {
    "general-cpu-pool": (
        "No general CPU pool is deployed (deployment.cpu_pools is empty), so a CPU stage that defaults to the "
        "general-cpu class has no live lane until fs2-general-cpu-batch-pool-terraform lands."
    ),
    "public-localization-delivery": (
        "Public localization generations live on the reference-data plane. Current main can mount that plane only "
        "whole at /reference-data; the general-cpu-lane integration head (003064c4) additionally allows exactly one "
        "read-only subPath generation mount at the adapter's declared path. Either way the mount needs the "
        "generation's content-addressed sub path, which only a localization receipt provides, so the "
        "runtime_artifacts localization_receipt_digest values below stay null until that receipt exists."
    ),
    "semantic-h100": (
        "The immutable image passed build-only H100 start checks with empty /models and /databases; no "
        "exact-artifact semantic inference has run, so the profile stays candidate-unqualified and unrouted."
    ),
    "artifact-localization": (
        "The runtime artifacts exist only as catalog manifests; no scientific-localization generation, marker or "
        "node admission receipt has been produced for them."
    ),
    "writable-cache": (
        "The declared compiler cache root needs a writable per-model claim; execution map v3 mounts only read-only "
        "reference/private sources, so the cache stays an unprovisioned auxiliary and no fast-start level above L1 "
        "is claimed."
    ),
}


@dataclass(frozen=True)
class StageShape:
    stage_id: str
    resource_class: str
    max_parallelism: int
    checkpoint_mode: str
    preemption_mode: str
    placement_class: str | None = None
    resources: Mapping[str, Any] | None = None
    execution_resources: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    module_name: str
    display_name: str
    operations: tuple[str, ...]
    service_classes: tuple[str, ...]
    mcp_tool_name: str
    mcp_description: str
    access_profile: str
    access_state: str
    commercial_use: str
    stages: tuple[StageShape, ...]
    limitations: tuple[str, ...]
    workload_namespace: str
    service_account_name: str
    gates: tuple[str, ...]
    artifact_catalog_ids: Mapping[str, str]
    contract_relative_paths: tuple[str, ...]


def _af3_stage_resources(
    cpu_millis: int, memory: int, ephemeral: int, *, limits: tuple[int, int, int]
) -> dict[str, Any]:
    return {
        "cpu_millis": cpu_millis,
        "memory_bytes": memory,
        "ephemeral_storage_bytes": ephemeral,
        "limits": {
            "cpu_millis": limits[0],
            "memory_bytes": limits[1],
            "ephemeral_storage_bytes": limits[2],
        },
    }


SPECS: dict[str, ModelSpec] = {
    "esmfold2": ModelSpec(
        model_id="esmfold2",
        module_name="esmfold2",
        display_name="ESMFold2 v3.4.0 (Biohub esm)",
        operations=("predict-structure",),
        service_classes=("interactive", "customer-batch"),
        mcp_tool_name="submit_esmfold2",
        mcp_description="Submit an ESMFold2 single-sequence or precomputed-MSA protein structure prediction.",
        access_profile="standard",
        access_state="not-required",
        commercial_use="allowed",
        stages=(
            StageShape("prepare-input", "cpu", 64, "restart", "restartable"),
            StageShape("fold", "gpu", 64, "restart", "restartable"),
        ),
        limitations=(
            (
                "Image sha256:e8fb269f...5463d is a build-only -h100-r4 candidate; it has no exact-artifact semantic "
                "H100 evidence."
            ),
            (
                "The trunk, ESMC-6B and CCD artifacts are catalog manifests without a localization generation or node "
                "admission receipt."
            ),
            (
                "precomputed-msa consumes MSA rows carried in the input manifest only; no online MSA server is ever "
                "contacted."
            ),
        ),
        workload_namespace="fs2-models",
        service_account_name="default",
        gates=(
            "semantic-h100",
            "artifact-localization",
            "public-localization-delivery",
            "general-cpu-pool",
        ),
        artifact_catalog_ids={
            "esmfold2-trunk": "esmfold2-trunk",
            "esmc-6b": "esmc-6b",
            "esmfold2-ccd": "esmfold2-ccd",
        },
        contract_relative_paths=(
            "components/control-plane/src/fs2_serve/scientific_batch/adapters/esmfold2.py",
            "catalog/runtime/schema/esmfold2-parameters.schema.json",
            "models/structure/batch-adapters/esmfold2/contract.json",
        ),
    ),
    "esmfold2-fast": ModelSpec(
        model_id="esmfold2-fast",
        module_name="esmfold2_fast",
        display_name="ESMFold2-Fast v3.4.0 (Biohub esm)",
        operations=("predict-protein-structure",),
        service_classes=("presentation", "interactive", "customer-batch"),
        mcp_tool_name="submit_esmfold2_fast",
        mcp_description="Submit a single-sequence ESMFold2-Fast protein structure prediction; MSA inputs are rejected.",
        access_profile="standard",
        access_state="not-required",
        commercial_use="allowed",
        stages=(
            StageShape("prepare-input", "cpu", 64, "restart", "restartable"),
            StageShape("fold", "gpu", 128, "restart", "restartable"),
        ),
        limitations=(
            (
                "Image sha256:ba55b9bb...a2577 is a build-only -h100-r4 candidate; it has no exact-artifact semantic "
                "H100 evidence."
            ),
            (
                "ESMFold2-Fast is a distinct model identity that shares code with ESMFold2 but not its trunk; every "
                "MSA "
                "mode is rejected before GPU admission."
            ),
            (
                "The fast trunk, ESMC-6B and CCD artifacts are catalog manifests without a localization generation or "
                "node admission receipt."
            ),
        ),
        workload_namespace="fs2-models",
        service_account_name="default",
        gates=(
            "semantic-h100",
            "artifact-localization",
            "public-localization-delivery",
            "general-cpu-pool",
        ),
        artifact_catalog_ids={
            "esmfold2-fast-trunk": "esmfold2-fast-trunk",
            "esmc-6b": "esmc-6b",
            "esmfold2-ccd": "esmfold2-ccd",
        },
        contract_relative_paths=(
            "components/control-plane/src/fs2_serve/scientific_batch/adapters/esmfold2_fast.py",
            "components/control-plane/src/fs2_serve/scientific_batch/adapters/esmfold2.py",
            "catalog/runtime/schema/esmfold2-fast-parameters.schema.json",
            "models/structure/batch-adapters/esmfold2-fast/contract.json",
        ),
    ),
    "protenix-v2": ModelSpec(
        model_id="protenix-v2",
        module_name="protenix_v2",
        display_name="Protenix v2.0.0",
        operations=("predict-complex-structure",),
        service_classes=("customer-batch",),
        mcp_tool_name="submit_protenix_v2",
        mcp_description="Submit a Protenix v2 biomolecular complex structure prediction on the offline no-MSA lane.",
        access_profile="standard",
        access_state="not-required",
        commercial_use="allowed",
        stages=(
            StageShape("prepare-data", "cpu", 64, "restart", "restartable"),
            StageShape("sample-structure", "gpu", 32, "restart", "restartable"),
        ),
        limitations=(
            (
                "Image sha256:27d816dc...8a644 is a build-only -h100-r4 candidate; it has no exact-artifact semantic "
                "H100 evidence."
            ),
            (
                "The v2 checkpoint was recovered from the immutable third-party mirror "
                "TMF001/protenix-v2-weights@653edab2 and is not yet byte-compared with the publisher CDN object."
            ),
            (
                "The adapter contract pins composite identity 5e1c3b54...74d48 while the artifact catalog records "
                "8e14bb80...12eca for the same five files; the localization producer must settle one identity before "
                "activation."
            ),
            (
                "Only msa_mode none is admitted; precomputed MSA waits for a validated relocation contract and no mode "
                "contacts an MSA server."
            ),
        ),
        workload_namespace="fs2-models",
        service_account_name="default",
        gates=(
            "semantic-h100",
            "artifact-localization",
            "public-localization-delivery",
            "general-cpu-pool",
            "writable-cache",
        ),
        artifact_catalog_ids={"protenix-v2": "protenix-v2"},
        contract_relative_paths=(
            "components/control-plane/src/fs2_serve/scientific_batch/adapters/protenix_v2.py",
            "catalog/runtime/schema/protenix-v2-parameters.schema.json",
            "models/structure/batch-adapters/protenix-v2/contract.json",
        ),
    ),
    "openfold3": ModelSpec(
        model_id="openfold3",
        module_name="openfold3",
        display_name="OpenFold3 OpenBind v0.5.0",
        operations=("predict-complex-structure",),
        service_classes=("customer-batch",),
        mcp_tool_name="submit_openfold3",
        mcp_description=(
            "Submit an OpenFold3 complex structure prediction; an independent backend that never satisfies an "
            "alphafold3 "
            "request."
        ),
        access_profile="standard",
        access_state="not-required",
        commercial_use="allowed",
        stages=(
            StageShape("data-pipeline", "cpu", 32, "none", "non_preemptible"),
            StageShape("inference", "gpu", 32, "restart", "restartable"),
        ),
        limitations=(
            (
                "Image sha256:d1d249fc...f203b is a build-only -h100-r4 candidate; it has no exact-artifact semantic "
                "H100 evidence."
            ),
            (
                "OpenFold3 is an independent, non-equivalent backend to AlphaFold 3: distinct code, weights, license "
                "and "
                "results; it is never reported as AlphaFold 3."
            ),
            (
                "The scientific model id openfold3 collides with the existing HTTP catalog record "
                "catalog/runtime/models/openfold3.json in the onboarding compiler's collision check; the integration "
                "owner must resolve that namespace before the profile is merged."
            ),
            (
                "The OpenBind-0 checkpoint and components.bcif are catalog manifests without a localization generation "
                "or node admission receipt."
            ),
        ),
        workload_namespace="fs2-models",
        service_account_name="default",
        gates=(
            "semantic-h100",
            "artifact-localization",
            "public-localization-delivery",
            "general-cpu-pool",
            "writable-cache",
        ),
        artifact_catalog_ids={
            "openfold3-openbind-0": "openfold3-openbind-0",
            "openfold3-components-bcif": "openfold3-components-bcif",
        },
        contract_relative_paths=(
            "components/control-plane/src/fs2_serve/scientific_batch/adapters/openfold3.py",
            "catalog/runtime/schema/openfold3-parameters.schema.json",
            "models/structure/batch-adapters/openfold3/contract.json",
        ),
    ),
    "alphafold3": ModelSpec(
        model_id="alphafold3",
        module_name="alphafold3",
        display_name="AlphaFold 3 v3.0.4",
        operations=("predict-complex-structure",),
        service_classes=("customer-batch",),
        mcp_tool_name="submit_alphafold3",
        mcp_description="Submit a deployment-authorized AlphaFold 3 structure prediction from a raw fold input.",
        access_profile="academic",
        access_state="verified",
        commercial_use="prohibited",
        stages=(
            StageShape(
                "data-pipeline",
                "cpu",
                1,
                "none",
                "non_preemptible",
                placement_class="reference-data",
                resources=_af3_stage_resources(16_000, 64 * GIB, 32 * GIB, limits=(16_000, 64 * GIB, 32 * GIB)),
                execution_resources={
                    "requests": {
                        "cpu": "16",
                        "memory": "64Gi",
                        "ephemeral_storage": "32Gi",
                    },
                    "limits": {
                        "cpu": "16",
                        "memory": "64Gi",
                        "ephemeral_storage": "32Gi",
                    },
                },
            ),
            StageShape(
                "inference",
                "gpu",
                1,
                "restart",
                "restartable",
                placement_class="accelerator",
                resources=_af3_stage_resources(8_000, 96 * GIB, 32 * GIB, limits=(32_000, 256 * GIB, 128 * GIB)),
                execution_resources={
                    "requests": {
                        "cpu": "8",
                        "memory": "96Gi",
                        "ephemeral_storage": "32Gi",
                    },
                    "limits": {
                        "cpu": "32",
                        "memory": "256Gi",
                        "ephemeral_storage": "128Gi",
                    },
                },
            ),
        ),
        limitations=(
            (
                "The exact r6 image is H100-qualified, but this route stays closed until one raw-input run completes "
                "through the public path and the integrated renderer is live-qualified."
            ),
            (
                "The licensed parameter file remains tenant-private and is authorized once by the deployment; "
                "callers do "
                "not provide a per-request license receipt. Formal institutional licence acceptance is still pending."
            ),
            (
                "Raw-input preprocessing runs only in the 16 CPU, 64 GiB reference-data class; the live reference pool "
                "nodes are 8 vCPU / 32 GiB and cannot admit it until a larger reference class exists."
            ),
            "OpenFold3 is an independent operational alternative and never satisfies an alphafold3 request.",
        ),
        workload_namespace="fs2-academic-poc",
        service_account_name="fs2-academic-runner",
        gates=(),
        artifact_catalog_ids={},
        contract_relative_paths=(
            "components/control-plane/src/fs2_serve/scientific_batch/adapters/alphafold3.py",
            "catalog/runtime/schema/alphafold3-parameters.schema.json",
            "models/structure/batch-adapters/alphafold3/adapter.py",
            "models/structure/batch-adapters/alphafold3/contract.json",
        ),
    ),
}

AF3_GATES = (
    "reference-cpu-class: the data-pipeline stage needs a 16 CPU / 64 GiB reference-data node class; both live "
    "reference nodes are 8 vCPU / 32 GiB behind a 6 CPU / 24Gi Kueue quota, so runnable_on_declared_pool is false "
    "until the general CPU lane provisions the larger class.",
    'allow-list: fs2_serve.scientific_batch.adapters must add "alphafold3" to its module allow-list tuple; that '
    "one-line shared edit moves the boltzgen and proteina-complexa recipe digests and is therefore left to the "
    "serialized integration step.",
    "academic-claim-ownership: the licensed object must be re-verified as gid 65532 / mode 0440 with "
    "supplementalGroups consumption before a GPU run; a 2026-09-03 fsGroup rewrite was reported and a repair was "
    "recorded by the BindCraft adapter task, but this task did not re-verify it live.",
    "formal-license: AlphaFold 3 formal institutional licence acceptance is FormalAcceptancePending; the authorized "
    "proof-of-concept path does not require it, and it is not synthesized here.",
    "cpu-class-record: scheduling/cpu-class-contract.json still names the AlphaFold 3 CPU stage raw-input; the "
    "controller gate and this fragment use data-pipeline, so the scheduling owner must rename the bound workload "
    "entry.",
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain one JSON object")
    return value


def _module(spec: ModelSpec) -> ModuleType:
    return importlib.import_module(f"fs2_serve.scientific_batch.adapters.{spec.module_name}")


def _contract(spec: ModelSpec) -> dict[str, Any]:
    return _load_json(ADAPTER_ROOT / spec.model_id / "contract.json")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def recipe_digest(relative_paths: Sequence[str]) -> str:
    """Replicate adapters.common.runtime_recipe_sha256 over an explicit path list."""

    from fs2_serve.scientific_batch.adapters import common

    digest = hashlib.sha256()
    for relative in sorted({*common._RECIPE_SHARED_PATHS, *relative_paths}):  # noqa: SLF001 - same algorithm
        content = (SOLUTION_ROOT / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _image(spec: ModelSpec, contract: Mapping[str, Any]) -> tuple[str, str]:
    image = contract["runtime_image"]
    if spec.model_id != "alphafold3":
        handoff = _load_json(IMAGE_HANDOFF)
        published = {item["model_id"]: item for item in handoff["images"]}[spec.model_id]
        if published["digest"] != image["digest"] or published["repository"] != image["repository"]:
            raise SystemExit(f"{spec.model_id} contract image differs from the published r4 handoff")
    return f"{image['repository']}@{image['digest']}", image["digest"]


def _stage_entry(shape: StageShape, module: ModuleType, previous: str | None) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "id": shape.stage_id,
        "needs": [] if previous is None else [previous],
        "resource_class": shape.resource_class,
        "admission_mode": "independent-jobs",
        "min_parallelism": 1,
        "max_parallelism": shape.max_parallelism,
        "checkpoint_mode": shape.checkpoint_mode,
        "preemption_mode": shape.preemption_mode,
    }
    if shape.placement_class is not None:
        stage["placement"] = {"class": shape.placement_class}
        stage["resources"] = dict(shape.resources or {})
    contracts = module.STAGE_EXECUTION_CONTRACTS
    if shape.stage_id not in contracts:
        raise SystemExit(f"{module.MODEL_ID} adapter declares no execution contract for stage {shape.stage_id}")
    return stage


def _af3_runtime_artifacts(module: ModuleType) -> list[dict[str, Any]]:
    return [
        {
            "artifact_id": module.PARAMETERS_ARTIFACT,
            "content_identity": {
                "digest_sha256": module.PARAMETERS_SHA256,
                "size_bytes": module.PARAMETERS_SIZE_BYTES,
            },
            "file_manifest": [
                {
                    "path": module.PARAMETERS_FILENAME,
                    "sha256": module.PARAMETERS_SHA256,
                    "size_bytes": module.PARAMETERS_SIZE_BYTES,
                }
            ],
            "required_files": [module.PARAMETERS_FILENAME],
        },
        {
            "artifact_id": module.REFERENCE_ARTIFACT,
            "content_identity": {
                "digest_sha256": module.REFERENCE_TREE_SHA256,
                "size_bytes": module.REFERENCE_EXPANDED_BYTES,
            },
            "file_manifest": [],
            "required_files": [],
            "readiness_manifest_sha256": module.REFERENCE_MANIFEST_SHA256,
        },
    ]


def render_profile(spec: ModelSpec) -> dict[str, Any]:
    module = _module(spec)
    contract = _contract(spec)
    _image_reference, image_digest = _image(spec, contract)
    stages: list[dict[str, Any]] = []
    previous: str | None = None
    for shape in spec.stages:
        stages.append(_stage_entry(shape, module, previous))
        previous = shape.stage_id
    workload = {
        "stages": stages,
        "retry": {"max_attempts": 2, "retryable_exit_codes": [137, 143]},
        "cancellation": {"mode": "terminate-attempt", "grace_seconds": 60},
    }
    profile: dict[str, Any] = {
        "schema": PROFILE_SCHEMA_ID,
        "model_id": module.MODEL_ID,
        "display_name": spec.display_name,
        "execution_mode": "scientific-batch",
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": "git",
            "repository": module.SOURCE_REPOSITORY,
            "revision": module.SOURCE_REVISION,
            "review_url": f"https://github.com/{module.SOURCE_REPOSITORY}/tree/{module.SOURCE_REVISION}",
            "classification": "candidate-input",
        },
        "execution_identity": {
            "model_revision": module.SOURCE_REVISION,
            "runtime_image_digest": image_digest,
            "runtime_recipe_sha256": recipe_digest(spec.contract_relative_paths),
            "workload_recipe_sha256": _canonical_sha256(workload),
            "artifact_manifest_digest": None,
            "execution_identity_sha256": None,
        },
        "interface": {
            "protocol": "scientific-batch-v1",
            "submit_endpoint": f"/v1/models/{module.MODEL_ID}:submit",
            "request_schema": REQUEST_SCHEMA,
            "result_schema": RESULT_SCHEMA,
            "parameter_schema": module.PARAMETER_SCHEMA,
            "operations": list(spec.operations),
            "service_classes": list(spec.service_classes),
            "mcp": {
                "discoverable": True,
                "invocable": False,
                "tool_name": spec.mcp_tool_name,
                "description": spec.mcp_description,
            },
        },
        "access": {
            "profile": spec.access_profile,
            "state": spec.access_state,
            "receipt_digest": None,
            "credentials_embedded": False,
        },
        "resources": {
            "gpu_count": 1,
            "gpu_topology": "single-gpu",
            "host_architectures": ["amd64"],
            "compatible_pool_ids": list(H100_POOLS),
            "required_node_labels": {ACCELERATOR_CLASS_LABEL: H100_CLASS},
        },
        "workload": workload,
        "semantic_validation": {
            "validator_id": module.VALIDATOR_ID,
            "state": "candidate-unqualified",
        },
        "policy": {
            "commercial_use": spec.commercial_use,
            "non_clinical": True,
            "limitations": list(spec.limitations),
        },
    }
    if spec.model_id == "alphafold3":
        profile["runtime_artifacts"] = _af3_runtime_artifacts(module)
    return {
        "schema": PROFILE_PROJECTION_SCHEMA,
        "merge_target": PROFILE_MERGE_TARGET,
        "rendered_by": "models/structure/batch-adapters/render_activation_fragments.py",
        "profile": profile,
    }


def _catalog_localization(artifact_id: str, mount_path: str) -> dict[str, Any]:
    manifest = _load_json(ARTIFACT_CATALOG / f"manifest-{artifact_id}.json")
    content = manifest["content"]
    files = [{"path": item["path"], "sha256": item["sha256"], "size_bytes": item["bytes"]} for item in content["files"]]
    return {
        "artifact_id": artifact_id,
        "mount_path": mount_path,
        "content_digest": f"sha256:{content['digest']}",
        "localization_receipt_digest": None,
        "file_manifest": files,
    }


def _workspace_mount() -> dict[str, Any]:
    return {
        "name": "artifact-workspace",
        "kind": "artifact-workspace",
        "claim_name": None,
        "host_path": None,
        "mount_path": "/mnt/fs2-scientific",
        "sub_path": None,
        "read_only": False,
    }


def _reference_mount(name: str = "reference-data") -> dict[str, Any]:
    return {
        "name": name,
        "kind": "reference",
        "claim_name": None,
        "host_path": REFERENCE_HOST_ROOT,
        "mount_path": REFERENCE_MOUNT_PATH,
        "sub_path": None,
        "read_only": True,
    }


def _execution_stage(
    spec: ModelSpec,
    shape: StageShape,
    module: ModuleType,
    image: str,
    mounts: list[dict[str, Any]],
) -> dict[str, Any]:
    contracts = module.STAGE_EXECUTION_CONTRACTS[shape.stage_id]
    labels: dict[str, str] = {}
    if shape.resource_class == "gpu":
        labels[ACCELERATOR_CLASS_LABEL] = H100_CLASS
    if any(mount["host_path"] is not None for mount in mounts):
        labels[REFERENCE_STORAGE_LABEL] = "true"
    if shape.execution_resources is not None:
        resources: Mapping[str, Any] = shape.execution_resources
    else:
        resources = GPU_STAGE_RESOURCES if shape.resource_class == "gpu" else CPU_STAGE_RESOURCES
    return {
        "stage_id": shape.stage_id,
        "image": image,
        "collector_id": contracts["collector_id"],
        "validator_id": contracts["validator_id"],
        "mounts": mounts,
        "service_account_name": spec.service_account_name,
        "resources": {
            "requests": dict(resources["requests"]),
            "limits": dict(resources["limits"]),
        },
        "active_deadline_seconds": STAGE_DEADLINE_SECONDS,
        "termination_grace_seconds": STAGE_GRACE_SECONDS,
        "environment": {},
        "required_node_labels": labels,
    }


def render_execution_fragment(spec: ModelSpec) -> dict[str, Any]:
    module = _module(spec)
    contract = _contract(spec)
    image, _digest = _image(spec, contract)
    runtime_artifacts: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    gates = [f"{key}: {SHARED_GATES[key]}" for key in spec.gates]
    if spec.model_id == "alphafold3":
        receipt = _load_json(AF3_TERMINAL_RECEIPT)
        receipt_digest = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if receipt_digest != module.REFERENCE_RECEIPT_SHA256:
            raise SystemExit("AlphaFold 3 terminal receipt digest differs from the adapter constant")
        stage_receipt_digest = hashlib.sha256(AF3_STAGE_RECEIPT.read_bytes()).hexdigest()
        runtime_artifacts = [
            {
                "artifact_id": module.PARAMETERS_ARTIFACT,
                "mount_path": module.PARAMETERS_MOUNT_PATH,
                "content_digest": f"sha256:{module.PARAMETERS_SHA256}",
                "localization_receipt_digest": f"sha256:{stage_receipt_digest}",
                "file_manifest": [
                    {
                        "path": module.PARAMETERS_FILENAME,
                        "sha256": module.PARAMETERS_SHA256,
                        "size_bytes": module.PARAMETERS_SIZE_BYTES,
                    }
                ],
            },
            {
                "artifact_id": module.REFERENCE_ARTIFACT,
                "mount_path": module.REFERENCE_MOUNT_PATH,
                "content_digest": f"sha256:{module.REFERENCE_TREE_SHA256}",
                "localization_receipt_digest": f"sha256:{module.REFERENCE_RECEIPT_SHA256}",
                "aggregate_tree": {
                    "storage_kind": "reference-data-plane",
                    "tree_sha256": module.REFERENCE_TREE_SHA256,
                    "manifest_sha256": module.REFERENCE_MANIFEST_SHA256,
                    "inventory_sha256": module.REFERENCE_INVENTORY_SHA256,
                    "manifest_algorithm": module.REFERENCE_MANIFEST_ALGORITHM,
                    "file_count": module.REFERENCE_FILE_COUNT,
                    "directory_count": 0,
                    "expanded_bytes": module.REFERENCE_EXPANDED_BYTES,
                    "canonical_path": module.REFERENCE_DATASET_SUB_PATH,
                    "marker_relative_path": module.REFERENCE_INVENTORY_MARKER,
                },
                "verification_receipt": receipt,
            },
        ]
        data_shape, inference_shape = spec.stages
        stages = [
            _execution_stage(
                spec,
                data_shape,
                module,
                image,
                [_workspace_mount(), _reference_mount("alphafold3-databases")],
            ),
            _execution_stage(
                spec,
                inference_shape,
                module,
                image,
                [
                    _workspace_mount(),
                    {
                        "name": "alphafold3-parameters",
                        "kind": "private",
                        "claim_name": module.PARAMETERS_CLAIM,
                        "host_path": None,
                        "mount_path": module.PARAMETERS_SOURCE_MOUNT_PATH,
                        "sub_path": module.PARAMETERS_CLAIM_SUB_PATH,
                        "read_only": True,
                    },
                ],
            ),
        ]
        gates.extend(AF3_GATES)
        state = "identities-complete-pending-serialized-integration"
    else:
        mounts_by_artifact = {item["artifact_id"]: item["mount_path"] for item in contract["runtime_artifacts"]}
        for artifact_id, catalog_id in spec.artifact_catalog_ids.items():
            runtime_artifacts.append(_catalog_localization(catalog_id, mounts_by_artifact[artifact_id]))
        for shape in spec.stages:
            stage_artifacts = module.STAGE_EXECUTION_CONTRACTS[shape.stage_id]["runtime_artifacts"]
            mounts = [_workspace_mount()]
            if stage_artifacts:
                mounts.append(_reference_mount())
            stages.append(_execution_stage(spec, shape, module, image, mounts))
        state = "stages-bound-runtime-artifacts-pending-localization"
    return {
        "schema": EXECUTION_FRAGMENT_SCHEMA,
        "merge_target": EXECUTION_MERGE_TARGET,
        "execution_map_schema": EXECUTION_MAP_SCHEMA,
        "rendered_by": "models/structure/batch-adapters/render_activation_fragments.py",
        "state": state,
        "model": {
            "model_id": module.MODEL_ID,
            "variant_id": module.VARIANT_ID,
            "workload_namespace": spec.workload_namespace,
            "access_profile": "academic" if spec.access_profile == "academic" else "public",
            "execution_identity_sha256": None,
            "plan_adapter": {
                "module": "fs2_serve.scientific_batch.adapters",
                "function": "compile_adapter_run",
            },
            "runtime_artifacts": runtime_artifacts,
            "stages": stages,
        },
        "activation_gates": gates,
    }


def render_integration_recipe(spec: ModelSpec) -> dict[str, Any]:
    module = _module(spec)
    shared_edits = [
        {
            "file": "catalog/runtime/contracts/scientific-workload-profiles.json",
            "edit": (
                "append activation/workload-profile.json#profile, then run "
                "components/control-plane/scripts/refresh_scientific_recipes.py after registering the model there"
            ),
        },
        {
            "file": "components/control-plane/src/fs2_serve/scientific_batch/adapters/common.py",
            "edit": f"add _RECIPE_MODEL_PATHS[{module.MODEL_ID!r}] = {json.dumps(list(spec.contract_relative_paths))}",
        },
        {
            "file": "components/control-plane/scripts/refresh_scientific_recipes.py",
            "edit": (
                "run it after the aggregate append; on the general-cpu-lane integration head it iterates every "
                "profile, refuses any model without a _RECIPE_MODEL_PATHS entry, and re-derives runtime_recipe_sha256, "
                "workload_recipe_sha256, execution_identity_sha256 and the execution-map identity chain "
                "(current main instead needs the model added to its MODEL_IDS tuple)"
            ),
        },
        {
            "file": "tests/test_scientific_workload_contracts.py",
            "edit": "extend the exact profile order, the image digest map and the parameter-schema fixture cases",
        },
        {
            "file": "components/control-plane/tests/test_scientific_canary.py",
            "edit": "raise the pinned profile_count once the profile is appended",
        },
        {
            "file": "catalog/runtime/contracts/scientific-execution-map.json",
            "edit": (
                "append activation/execution-map-fragment.json#model only after every runtime_artifacts "
                "localization_receipt_digest is a real receipt digest"
            ),
        },
    ]
    if spec.model_id == "alphafold3":
        shared_edits.insert(
            0,
            {
                "file": "components/control-plane/src/fs2_serve/scientific_batch/adapters/__init__.py",
                "edit": 'add "alphafold3" to the _register_legacy_primary module tuple',
            },
        )
        shared_edits.append(
            {
                "file": "scheduling/cpu-class-contract.json",
                "edit": "rename the alphafold3 bound workload stage raw-input to data-pipeline (scheduling owner)",
            }
        )
    return {
        "schema": RECIPE_SCHEMA,
        "model_id": module.MODEL_ID,
        "variant_id": module.VARIANT_ID,
        "recipe_paths": list(spec.contract_relative_paths),
        "runtime_recipe_sha256_at_render": recipe_digest(spec.contract_relative_paths),
        "recipe_note": (
            "Computed with adapters.common.runtime_recipe_sha256's algorithm over the shared execution contracts plus "
            "recipe_paths. Any edit to a shared execution contract moves it, so the deterministic refresh is to rerun "
            "models/structure/batch-adapters/render_activation_fragments.py on the integration head and then "
            "refresh_scientific_recipes.py after the aggregate append; the value here proves the fragment matched the "
            "code on this branch head."
        ),
        "shared_edits": shared_edits,
        "no_deploy": "This recipe changes no live cluster, Terraform state, quota or shared service.",
    }


def render_all(spec: ModelSpec) -> dict[str, dict[str, Any]]:
    return {
        "workload-profile.json": render_profile(spec),
        "execution-map-fragment.json": render_execution_fragment(spec),
        "integration-recipe.json": render_integration_recipe(spec),
    }


def _serialize(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail when a committed fragment differs")
    parser.add_argument(
        "--model",
        action="append",
        choices=sorted(SPECS),
        help="limit to one or more models",
    )
    options = parser.parse_args(argv)
    drifted: list[str] = []
    for model_id in options.model or sorted(SPECS):
        spec = SPECS[model_id]
        directory = ADAPTER_ROOT / model_id / "activation"
        for filename, document in render_all(spec).items():
            path = directory / filename
            rendered = _serialize(document)
            if options.check:
                if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
                    drifted.append(str(path.relative_to(SOLUTION_ROOT)))
                continue
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            print(f"rendered {path.relative_to(SOLUTION_ROOT)}")
    if drifted:
        for item in drifted:
            print(f"drift: {item}")
        print("run models/structure/batch-adapters/render_activation_fragments.py to refresh them")
        return 1
    if options.check:
        print("activation fragments are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
