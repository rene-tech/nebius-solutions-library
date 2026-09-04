"""Cross-contract tests for the current-controller AlphaFold 3 adapter.

The adapter is model-owned and is not yet in the dispatcher allow-list, so the
suite registers it through the same ``_register_legacy_primary`` seam the
integration step will use.  Every identity is checked against the accepted
contracts elsewhere in the tree: the r6 image lock and command contract, the
academic parameter binding, the published reference-data terminal receipt, the
CPU class record and the model-owned activation fragments.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

from fs2_serve.crypto import KeyedHasher
from fs2_serve.scientific_batch import (
    CheckpointMode,
    MaterializationMode,
    PreemptionMode,
    ResourceClass,
    SchedulingSnapshot,
    ScientificAdapterError,
    ScientificBatchController,
    ServiceClass,
    StageSchedulingDecision,
    compile_adapter_run,
)
from fs2_serve.scientific_batch import adapters as adapter_registry
from fs2_serve.scientific_batch.adapters import alphafold3 as af3
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.models import (
    ArtifactAccessContext,
    ResolvedArtifactMaterialization,
    ScientificInputArtifact,
    StagePlacementClass,
    VerifiedInputManifest,
    WorkloadKind,
    WorkloadResource,
)
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = SOLUTION_ROOT / "catalog/runtime"
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters/alphafold3"
ACTIVATION_ROOT = ADAPTER_ROOT / "activation"
PARAMETER_SCHEMA_PATH = CATALOG_ROOT / "schema/alphafold3-parameters.schema.json"
PROFILE_SCHEMA_PATH = CATALOG_ROOT / "schema/scientific-workload-profile.schema.json"
IMAGE_ROOT = SOLUTION_ROOT / "models/cancer-immunotherapy/images/alphafold3"
COMMAND_CONTRACT_PATH = IMAGE_ROOT / "contracts/af3-command-io-contract.json"
RUNTIME_HANDOFF_PATH = IMAGE_ROOT / "contracts/af3-runtime-handoff.json"
PARAMETER_BINDING_PATH = IMAGE_ROOT / "contracts/af3-parameter-binding.json"
ACADEMIC_READINESS_PATH = CATALOG_ROOT / "contracts/academic-asset-readiness.json"
CPU_CLASS_PATH = SOLUTION_ROOT / "scheduling/cpu-class-contract.json"
REFERENCE_REQUIREMENTS_PATH = SOLUTION_ROOT / "reference-data/model-requirements.json"
TERMINAL_RECEIPT_PATH = SOLUTION_ROOT / "reference-data/evidence/af3-terminal-receipt-20260903.json"
NOW = datetime(2026, 9, 4, tzinfo=UTC)


@pytest.fixture(autouse=True, scope="module")
def _register_alphafold3() -> None:
    """Register through the allow-list seam the integration edit will use."""

    if af3.MODEL_ID not in adapter_registry._COMPILERS:  # noqa: SLF001 - the seam under test
        adapter_registry._register_legacy_primary("alphafold3")  # noqa: SLF001


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def sha(value: str) -> str:
    """Bare hex digest, the form profile documents carry."""

    return hashlib.sha256(value.encode()).hexdigest()


def digest(value: str) -> str:
    """Prefixed digest, the form controller models carry."""

    return f"sha256:{sha(value)}"


def request(name: str = "positive-raw.json") -> dict[str, Any]:
    return load(ADAPTER_ROOT / "fixtures" / name)


def profile() -> Mapping[str, object]:
    return load(ACTIVATION_ROOT / "workload-profile.json")["profile"]


def execution_fragment() -> dict[str, Any]:
    return load(ACTIVATION_ROOT / "execution-map-fragment.json")


def reference_data_producer() -> ModuleType:
    path = SOLUTION_ROOT / "reference-data/reference_data.py"
    spec = importlib.util.spec_from_file_location("af3_reference_data_producer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def runtime_producer() -> ModuleType:
    path = IMAGE_ROOT / "runtime/af3_runtime.py"
    name = "af3_runtime_for_current_adapter"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _receipt(mode: str, *, status: str = "PASS") -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": af3.RUNTIME_RECEIPT_SCHEMA,
        "mode": mode,
        "image": {"runtime_id": af3.MODEL_ID, "upstream_commit": af3.SOURCE_REVISION},
        "status": status,
        "execution": {
            "upstream": "/app/alphafold/run_alphafold.py",
            "exit_code": 0 if status == "PASS" else 1,
            "terminal_state": "succeeded" if status == "PASS" else "failed",
        },
    }
    if mode == "inference":
        receipt["parameters"] = {
            "artifact_id": af3.PARAMETERS_ARTIFACT,
            "sha256": af3.PARAMETERS_SHA256,
            "size_bytes": af3.PARAMETERS_SIZE_BYTES,
            "path": af3.PARAMETERS_MOUNT_PATH,
            "read_only_mount": True,
        }
    return receipt


def data_workspace(tmp_path: Path, *, status: str = "PASS") -> Path:
    output = tmp_path / af3.DATA_OUTPUT_DIR
    job = output / "pdl1-binder"
    job.mkdir(parents=True)
    payload = {"name": "pdl1-binder", "sequences": [{"protein": {"sequence": "ACDE"}}]}
    (job / "pdl1-binder_data.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    runtime_producer().build_data_handoff(output)
    (tmp_path / af3.DATA_RECEIPT_FILENAME).write_text(
        json.dumps(_receipt("data", status=status), separators=(",", ":")),
        encoding="utf-8",
    )
    return tmp_path


def _mmcif(seed: int) -> bytes:
    rows = [
        f"ATOM {index} C CA A {index + seed / 1000:.3f} {index * 1.5:.3f} {index + 1:.3f}" for index in range(1, 13)
    ]
    return (
        "data_prediction\n#\nloop_\n_atom_site.group_PDB\n_atom_site.id\n"
        "_atom_site.type_symbol\n_atom_site.label_atom_id\n_atom_site.label_asym_id\n"
        "_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\n" + "\n".join(rows) + "\n#\n"
    ).encode("ascii")


def _summary(ranking: float) -> str:
    return json.dumps(
        {
            "ptm": 0.83,
            "iptm": None,
            "ranking_score": ranking,
            "fraction_disordered": 0.04,
            "has_clash": 0.0,
            "num_recycles": 10,
            "chain_ptm": [0.83],
        }
    )


def inference_workspace(tmp_path: Path, *, jobs: tuple[str, ...] = ("pdl1-binder",), samples: int = 2) -> Path:
    """Deterministic fixture mirroring the upstream AlphaFold 3 output layout."""

    outputs = tmp_path / af3.INFERENCE_OUTPUT_DIR
    for job in jobs:
        job_dir = outputs / job
        job_dir.mkdir(parents=True)
        (job_dir / f"{job}_model.cif").write_bytes(_mmcif(1))
        (job_dir / f"{job}_summary_confidences.json").write_text(_summary(0.91), encoding="utf-8")
        (job_dir / f"{job}_confidences.json").write_text(
            json.dumps({"atom_plddts": [90.1] * 12, "pae": [[0.5]]}), encoding="utf-8"
        )
        rows = ["seed,sample,ranking_score"]
        for sample in range(samples):
            sample_dir = job_dir / f"seed-1_sample-{sample}"
            sample_dir.mkdir()
            (sample_dir / "model.cif").write_bytes(_mmcif(2 + sample))
            (sample_dir / "summary_confidences.json").write_text(_summary(0.9 - sample / 100), encoding="utf-8")
            (sample_dir / "confidences.json").write_text("{}", encoding="utf-8")
            rows.append(f"1,{sample},{0.9 - sample / 100:.3f}")
        (job_dir / "ranking_scores.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (job_dir / "TERMS_OF_USE.md").write_text("terms\n", encoding="utf-8")
    (tmp_path / af3.INFERENCE_RECEIPT_FILENAME).write_text(
        json.dumps(_receipt("inference"), separators=(",", ":")), encoding="utf-8"
    )
    return tmp_path


def scheduling(plan: Any, *, captured_at: datetime) -> SchedulingSnapshot:
    decisions: list[StageSchedulingDecision] = []
    for stage in plan.controller_plan.stages:
        if stage.stage_id == af3.DATA_STAGE_ID:
            decisions.append(
                StageSchedulingDecision(
                    stage_id=stage.stage_id,
                    resource_class=ResourceClass.CPU,
                    resolved_cluster_queue=af3.DATA_CLUSTER_QUEUE,
                    resolved_local_queue=af3.DATA_LOCAL_QUEUE,
                    workload_priority_class="fs2-customer-batch",
                    workload_priority_value=100,
                    resolved_pool_preference=("reference-cpu",),
                    accelerator_resource_name=None,
                    accelerator_count=0,
                    max_queue_seconds=3600,
                    max_execution_seconds=43200,
                    checkpoint_mode=stage.checkpoint_mode,
                    preemption_mode=stage.preemption_mode,
                    placement_class=StagePlacementClass.REFERENCE_DATA_CPU,
                    workload_namespace=af3.EXECUTION_NAMESPACE,
                    route_namespace=af3.EXECUTION_NAMESPACE,
                    requested_resource_flavor="reference-data-cpu",
                    node_selector=(("storage.fs2.nebius/reference-data", "true"),),
                )
            )
        else:
            decisions.append(
                StageSchedulingDecision(
                    stage_id=stage.stage_id,
                    resource_class=ResourceClass.GPU,
                    resolved_cluster_queue=af3.INFERENCE_CLUSTER_QUEUE,
                    resolved_local_queue=af3.INFERENCE_LOCAL_QUEUE,
                    workload_priority_class="fs2-customer-batch",
                    workload_priority_value=100,
                    resolved_pool_preference=("h100-reserved-8x", "h100-1x"),
                    admitted_resource_flavor="inference-h100",
                    accelerator_resource_name="nvidia.com/gpu",
                    accelerator_count=1,
                    max_queue_seconds=3600,
                    max_execution_seconds=43200,
                    checkpoint_mode=stage.checkpoint_mode,
                    preemption_mode=stage.preemption_mode,
                    placement_class=StagePlacementClass.ACCELERATOR,
                    workload_namespace=af3.EXECUTION_NAMESPACE,
                    route_namespace=af3.EXECUTION_NAMESPACE,
                )
            )
    return SchedulingSnapshot(
        policy_revision="sha256:" + "5" * 64,
        captured_at=captured_at,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="academic-scientific",
        model_lane="alphafold3",
        workload_namespace=af3.EXECUTION_NAMESPACE,
        route_namespace=af3.EXECUTION_NAMESPACE,
        stages=tuple(decisions),
    )


def test_fragment_profile_is_a_schema_valid_unrouted_academic_candidate() -> None:
    candidate = profile()
    validator = Draft202012Validator(load(PROFILE_SCHEMA_PATH), format_checker=FormatChecker())
    assert list(validator.iter_errors(candidate)) == []
    assert candidate["route_exposed"] is False
    assert candidate["state"] == "candidate-unqualified"
    assert candidate["access"] == {  # type: ignore[comparison-overlap]
        "profile": "academic",
        "state": "verified",
        "receipt_digest": None,
        "credentials_embedded": False,
    }
    identity = candidate["execution_identity"]
    assert identity["runtime_image_digest"] == af3.RUNTIME_IMAGE_DIGEST  # type: ignore[index]
    assert identity["model_revision"] == af3.SOURCE_REVISION  # type: ignore[index]
    assert "eaea560c" not in json.dumps(candidate)
    assert ScientificWorkloadProfile(MappingProxyType(dict(candidate))).runnable is False
    workload = candidate["workload"]
    workload_hash = hashlib.sha256(json.dumps(workload, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    assert identity["workload_recipe_sha256"] == workload_hash  # type: ignore[index]
    stages = {stage["id"]: stage for stage in workload["stages"]}  # type: ignore[index]
    assert list(stages) == [af3.DATA_STAGE_ID, af3.INFERENCE_STAGE_ID]
    assert stages[af3.DATA_STAGE_ID]["placement"] == {"class": "reference-data"}
    assert stages[af3.DATA_STAGE_ID]["resources"]["cpu_millis"] == af3.DATA_CPU_MILLIS
    assert stages[af3.DATA_STAGE_ID]["resources"]["memory_bytes"] == af3.DATA_MEMORY_BYTES
    assert stages[af3.INFERENCE_STAGE_ID]["placement"] == {"class": "accelerator"}
    artifacts = {item["artifact_id"]: item for item in candidate["runtime_artifacts"]}  # type: ignore[union-attr]
    assert artifacts[af3.PARAMETERS_ARTIFACT]["content_identity"] == {
        "digest_sha256": af3.PARAMETERS_SHA256,
        "size_bytes": af3.PARAMETERS_SIZE_BYTES,
    }
    assert artifacts[af3.REFERENCE_ARTIFACT]["content_identity"] == {
        "digest_sha256": af3.REFERENCE_TREE_SHA256,
        "size_bytes": af3.REFERENCE_EXPANDED_BYTES,
    }
    assert artifacts[af3.REFERENCE_ARTIFACT]["readiness_manifest_sha256"] == af3.REFERENCE_MANIFEST_SHA256


def test_public_parameters_schema_is_registered_and_rejects_caller_authorization() -> None:
    schema = load(PARAMETER_SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(request()["parameters"]))
    assert not list(validator.iter_errors(request("positive-selected-job.json")["parameters"]))
    assert list(validator.iter_errors(request("negative-request-license.json")["parameters"]))
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    assert af3.PARAMETER_SCHEMA in catalog._validators  # type: ignore[attr-defined]  # noqa: SLF001


def test_image_identity_matches_the_accepted_r6_lock_and_runtime_handoff() -> None:
    lock = load(IMAGE_ROOT / "contracts/af3-image-lock.json")
    handoff = load(RUNTIME_HANDOFF_PATH)
    assert handoff["image"]["digest"] == af3.RUNTIME_IMAGE_DIGEST
    assert handoff["image"]["tag"] == af3.RUNTIME_IMAGE_TAG
    assert handoff["readiness"]["state"] == "runtime-qualified"
    assert tuple(handoff["entrypoint"]["command"]) == af3.RUNTIME_COMMAND
    assert json.dumps(lock).count(af3.RUNTIME_IMAGE_DIGEST) >= 1
    superseded = {item["digest"] for item in handoff["image"]["superseded"]}
    assert af3.RUNTIME_IMAGE_DIGEST not in superseded
    assert af3.RUNTIME_IMAGE == f"{af3.RUNTIME_IMAGE_REPOSITORY}@{af3.RUNTIME_IMAGE_DIGEST}"


def test_commands_are_the_exact_supported_runtime_surface() -> None:
    contract = load(COMMAND_CONTRACT_PATH)
    plan = compile_adapter_run(
        af3.MODEL_ID, profile(), request(), operation_id="op-af3-command-contract", variant_id=af3.VARIANT_ID
    )
    data = plan.invocation(af3.DATA_STAGE_ID, "main")
    inference = plan.invocation(af3.INFERENCE_STAGE_ID, "main")
    assert tuple(contract["entrypoint"]["command"]) == af3.RUNTIME_COMMAND
    assert data.argv[:3] == (*af3.RUNTIME_COMMAND, "data")
    assert inference.argv[:3] == (*af3.RUNTIME_COMMAND, "inference")
    assert data.argv[data.argv.index("--receipt") + 1].endswith(f"/{af3.DATA_RECEIPT_FILENAME}")
    assert inference.argv[inference.argv.index("--receipt") + 1].endswith(f"/{af3.INFERENCE_RECEIPT_FILENAME}")
    assert contract["legacy_aliases"]["fs2-run-alphafold3"]["supported"] is False
    allowed = {
        flag
        for stage in contract["stages"].values()
        for key in ("runtime_args", "optional_runtime_args")
        for flag in stage[key]
        if flag.startswith("--")
    }
    emitted = {value for invocation in plan.invocations for value in invocation.argv if value.startswith("--")}
    assert emitted <= allowed
    assert all(invocation.argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"} for invocation in plan.invocations)
    serialized = "\0".join(value for invocation in plan.invocations for value in invocation.argv)
    assert all(token not in serialized for token in af3.FORBIDDEN_ARGV_TOKENS)
    selected = compile_adapter_run(
        af3.MODEL_ID, profile(), request("positive-selected-job.json"), operation_id="op-af3-selected-job"
    ).invocation(af3.INFERENCE_STAGE_ID, "main")
    assert selected.argv[selected.argv.index("--fold-job") + 1] == "pdl1-binder"


def test_reference_root_and_private_parameter_file_are_never_co_mounted() -> None:
    command = load(COMMAND_CONTRACT_PATH)
    adapter_contract = load(ADAPTER_ROOT / "contract.json")
    readiness = load(ACADEMIC_READINESS_PATH)
    binding = load(PARAMETER_BINDING_PATH)
    academic = next(item for item in readiness["models"] if item["model_id"] == af3.MODEL_ID)

    data_mount = af3.mount_contract(af3.DATA_STAGE_ID)
    inference_mount = af3.mount_contract(af3.INFERENCE_STAGE_ID)
    assert af3.REFERENCE_HOST_ROOT == command["root_layout"]["reference_root"]["host_root"]
    assert data_mount.mount_path == command["root_layout"]["reference_root"]["mount_path"]
    assert data_mount.sub_path is None
    assert data_mount.supplemental_groups == (af3.REFERENCE_DATA_GID,)
    assert data_mount.expected_content_sha256 == af3.REFERENCE_TREE_SHA256
    assert data_mount.expected_manifest_sha256 == af3.REFERENCE_MANIFEST_SHA256
    assert command["root_layout"]["reference_root"]["single_mount"] is True
    assert command["root_layout"]["parameters"]["mount_path"] == af3.PARAMETERS_MOUNT_PATH

    private = academic["runtime_binding"]
    assert inference_mount.artifact_id == private["artifact_id"]
    assert inference_mount.mount_path == private["consumer_path"]
    assert af3.PARAMETERS_SOURCE_SUB_PATH == private["source_sub_path"]
    assert inference_mount.sub_path == af3.PARAMETERS_FILENAME
    assert inference_mount.expected_content_sha256 == private["content_digest_sha256"]
    assert af3.PARAMETERS_SIZE_BYTES == private["content_bytes"]
    assert af3.PARAMETERS_CLAIM == readiness["delivery"]["claim"] == binding["delivery"]["claim"]
    assert af3.PARAMETERS_CLAIM_NAMESPACE == readiness["delivery"]["namespace"]
    assert inference_mount.supplemental_groups == (academic["delivery"]["asset_gid"],)
    assert binding["license"]["embed_in_image"] is False
    assert binding["license"]["world_readable"] is False
    assert binding["delivery"]["permissions"]["fs_group_forbidden"] is True
    preferred = next(mode for mode in binding["delivery"]["supported_modes"] if mode["preferred"])
    assert preferred["source_sub_path"] == af3.PARAMETERS_SOURCE_SUB_PATH
    assert preferred["consumer_path"] == af3.PARAMETERS_MOUNT_PATH

    stages = {item["stage_id"]: item for item in adapter_contract["stages"]}
    assert stages[af3.DATA_STAGE_ID]["forbidden_artifacts"] == [af3.PARAMETERS_ARTIFACT]
    assert stages[af3.INFERENCE_STAGE_ID]["forbidden_artifacts"] == [af3.REFERENCE_ARTIFACT]

    plan = compile_adapter_run(af3.MODEL_ID, profile(), request(), operation_id="op-af3-planes")
    data = plan.invocation(af3.DATA_STAGE_ID, "main")
    inference = plan.invocation(af3.INFERENCE_STAGE_ID, "main")
    assert data.runtime_artifacts == (af3.REFERENCE_ARTIFACT,)
    assert inference.runtime_artifacts == (af3.PARAMETERS_ARTIFACT,)
    assert data.runtime_mounts == (data_mount,)
    assert inference.runtime_mounts == (inference_mount,)
    assert data.runtime_trees == inference.runtime_trees == ()
    assert af3.PARAMETERS_MOUNT_PATH not in data.argv
    assert af3.REFERENCE_MOUNT_PATH not in inference.argv
    assert data.handoff_name == af3.HANDOFF_NAME
    assert (data.collector_id, data.validator_id) == (af3.DATA_COLLECTOR_ID, af3.DATA_VALIDATOR_ID)
    assert (inference.collector_id, inference.validator_id) == (af3.RESULT_COLLECTOR_ID, af3.VALIDATOR_ID)
    with pytest.raises(ValueError, match="approved image root"):
        replace(data_mount, mount_path="/outside-approved-root/reference-data")
    with pytest.raises(ValueError, match="exactly cover"):
        replace(data, runtime_mounts=(data_mount, inference_mount))


def test_reference_identities_are_the_published_terminal_receipt() -> None:
    producer = reference_data_producer()
    receipt = producer.validate_terminal_receipt(load(TERMINAL_RECEIPT_PATH))
    digest = producer.sha256_bytes(producer.canonical_json(receipt))
    assert digest == af3.REFERENCE_RECEIPT_SHA256
    assert receipt["bundle_id"] == af3.REFERENCE_ARTIFACT
    assert receipt["revision"] == af3.REFERENCE_REVISION
    assert receipt["storage"]["host_root"] == af3.REFERENCE_HOST_ROOT
    assert receipt["storage"]["mount_path"] == af3.REFERENCE_MOUNT_PATH
    assert receipt["storage"]["dataset_sub_path"] == af3.REFERENCE_DATASET_SUB_PATH
    assert receipt["content"]["tree_sha256"] == af3.REFERENCE_TREE_SHA256
    assert receipt["content"]["manifest_sha256"] == af3.REFERENCE_MANIFEST_SHA256
    assert receipt["content"]["inventory_sha256"] == af3.REFERENCE_INVENTORY_SHA256
    assert receipt["content"]["file_count"] == af3.REFERENCE_FILE_COUNT
    assert receipt["content"]["expanded_bytes"] == af3.REFERENCE_EXPANDED_BYTES
    assert receipt["content"]["inventory_marker"] == af3.REFERENCE_INVENTORY_MARKER
    assert receipt["placement"]["resource_class"] == "cpu"
    assert producer.derive_database_root(receipt) == f"{af3.REFERENCE_MOUNT_PATH}/{af3.REFERENCE_DATASET_SUB_PATH}"
    invocation = compile_adapter_run(af3.MODEL_ID, profile(), request(), operation_id="op-af3-reference").invocation(
        af3.DATA_STAGE_ID, "main"
    )
    assert invocation.argv[invocation.argv.index("--reference-receipt") + 1] == af3.REFERENCE_RECEIPT_PATH
    assert (
        af3.REFERENCE_RECEIPT_PATH
        == f"{af3.REFERENCE_MOUNT_PATH}/receipts/{receipt['bundle_id']}/{receipt['revision']}.json"
    )


def test_current_controller_stage_identity_and_cpu_envelope_are_exact() -> None:
    cpu_contract = load(CPU_CLASS_PATH)
    bound = [
        item
        for item in cpu_contract["classes"]["reference-data"]["bound_workloads"]
        if item["model_id"] == af3.MODEL_ID and item["capacity"] == {"cpu": af3.DATA_CPU, "memory": af3.DATA_MEMORY}
    ]
    assert len(bound) == 1
    # The scheduling record still names the stage raw-input; the controller gate
    # and this adapter use data-pipeline.  The rename is recorded as a gate.
    assert bound[0]["stage"] == "raw-input"
    assert any("cpu-class-record" in gate for gate in execution_fragment()["activation_gates"])
    requirements = load(REFERENCE_REQUIREMENTS_PATH)["models"][af3.MODEL_ID]["preprocessing_capacity"]
    assert requirements["cpu"] == af3.DATA_CPU
    assert requirements["memory"] == af3.DATA_MEMORY
    assert requirements["ephemeral_storage"] == af3.DATA_EPHEMERAL_STORAGE
    plan = compile_adapter_run(af3.MODEL_ID, profile(), request(), operation_id="op-af3-scheduler")
    stages = plan.controller_plan.stages
    assert [stage.stage_id for stage in stages] == [af3.DATA_STAGE_ID, af3.INFERENCE_STAGE_ID]
    assert [stage.resource_class for stage in stages] == [ResourceClass.CPU, ResourceClass.GPU]
    data, inference = stages
    assert data.placement_class is StagePlacementClass.REFERENCE_DATA_CPU
    assert data.resources is not None and data.resources.cpu_millis == af3.DATA_CPU_MILLIS
    assert data.resources.memory_bytes == af3.DATA_MEMORY_BYTES
    assert data.checkpoint_mode is CheckpointMode.NONE
    assert data.preemption_mode is PreemptionMode.NON_PREEMPTIBLE
    assert inference.placement_class is StagePlacementClass.ACCELERATOR
    assert inference.depends_on == (af3.DATA_STAGE_ID,)
    assert inference.checkpoint_mode is CheckpointMode.RESTART
    assert inference.preemption_mode is PreemptionMode.RESTARTABLE


def test_authorization_is_deployment_bound_and_request_has_no_receipt() -> None:
    readiness = load(ACADEMIC_READINESS_PATH)
    academic = next(item for item in readiness["models"] if item["model_id"] == af3.MODEL_ID)
    assert academic["use_authorization_status"] == "Granted"
    assert academic["execution_authorization_status"] == "Authorized"
    assert academic["serving_admission"] == "AdmittedNoPerRequestLicenseReceipt"
    assert academic["formal_license_status"] == "FormalAcceptancePending"
    assert academic["alternative"]["model_id"] == "openfold3"
    assert readiness["request_time_license_receipt_required"] is False

    ordinary_request = request()
    assert "license" not in json.dumps(ordinary_request).lower()
    compile_adapter_run(af3.MODEL_ID, profile(), ordinary_request, operation_id="op-af3-authorized")

    promoted = copy.deepcopy(dict(profile()))
    promoted["access"]["receipt_digest"] = "f" * 64  # type: ignore[index]
    compile_adapter_run(af3.MODEL_ID, promoted, ordinary_request, operation_id="op-af3-deployment-receipt")

    for field, value in (
        ("profile", "standard"),
        ("state", "blocked"),
        ("receipt_digest", "not-a-digest"),
        ("credentials_embedded", True),
    ):
        denied = copy.deepcopy(dict(profile()))
        denied["access"][field] = value  # type: ignore[index]
        with pytest.raises(ScientificAdapterError, match=af3.ADMISSION_BLOCKER):
            compile_adapter_run(af3.MODEL_ID, denied, ordinary_request, operation_id=f"op-denied-{field}")

    with pytest.raises(ScientificAdapterError, match="unknown.*license_receipt"):
        compile_adapter_run(
            af3.MODEL_ID, profile(), request("negative-request-license.json"), operation_id="op-af3-caller-license"
        )


def test_handoff_is_operation_isolated_and_portable_between_stages() -> None:
    first = compile_adapter_run(af3.MODEL_ID, profile(), request(), operation_id="op-af3-one")
    second = compile_adapter_run(af3.MODEL_ID, profile(), request(), operation_id="op-af3-two")
    data = first.invocation(af3.DATA_STAGE_ID, "main")
    inference = first.invocation(af3.INFERENCE_STAGE_ID, "main")
    assert data.working_directory != inference.working_directory
    assert {item.working_directory for item in first.invocations}.isdisjoint(
        {item.working_directory for item in second.invocations}
    )
    assert data.materializations[0].mode is MaterializationMode.COPY_FILE
    assert data.materializations[0].destination.endswith(f"/input/{af3.FOLD_INPUT_FILENAME}")
    assert inference.consumes == (data.produces,)
    assert inference.materializations[0].artifact_id == data.produces
    assert inference.materializations[0].mode is MaterializationMode.EXTRACT_TAR
    assert inference.materializations[0].destination.endswith(f"/{af3.HANDOFF_MOUNT_DIR}")
    assert data.working_directory not in inference.argv
    assert f"/{af3.HANDOFF_DIR_NAME}" not in "\0".join(inference.argv)


def test_data_collector_accepts_the_runtime_handoff_and_is_reproducible(tmp_path: Path) -> None:
    workspace = data_workspace(tmp_path)
    first = af3.collect_data(workspace)
    second = af3.collect_stage_output(af3.DATA_COLLECTOR_ID, request(), workspace)
    assert first == second
    entry = first.manifest["entries"][0]  # type: ignore[index]
    pointer = entry["artifact"]  # type: ignore[index]
    blob = first.blobs[pointer["artifact_id"]]  # type: ignore[index]
    assert entry["name"] == af3.HANDOFF_NAME  # type: ignore[index]
    assert entry["semantic_type"] == "alphafold3-data-handoff/v1"  # type: ignore[index]
    assert pointer["sha256"] == hashlib.sha256(blob).hexdigest()  # type: ignore[index]
    assert pointer["size_bytes"] == len(blob)  # type: ignore[index]
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [af3.HANDOFF_INDEX, "pdl1-binder/pdl1-binder_data.json"]
        assert all(not member.name.startswith("/") and ".." not in Path(member.name).parts for member in members)
        assert all(member.uid == member.gid == member.mtime == 0 and member.mode == 0o440 for member in members)


def test_data_collector_refuses_failed_tampered_escaped_or_linked_output(tmp_path: Path) -> None:
    with pytest.raises(ScientificAdapterError, match="terminal PASS"):
        af3.collect_data(data_workspace(tmp_path / "failed", status="FAIL"))
    tampered = data_workspace(tmp_path / "tampered")
    payload = tampered / af3.DATA_OUTPUT_DIR / af3.HANDOFF_DIR_NAME / "pdl1-binder" / "pdl1-binder_data.json"
    payload.write_bytes(payload.read_bytes() + b"\n")
    with pytest.raises(ScientificAdapterError, match="digest or size"):
        af3.collect_data(tampered)
    escaped = data_workspace(tmp_path / "escaped")
    index_path = escaped / af3.DATA_OUTPUT_DIR / af3.HANDOFF_DIR_NAME / af3.HANDOFF_INDEX
    index = load(index_path)
    index["entries"][0]["fold_job"] = ".."
    index["entries"][0]["relative_path"] = "../.._data.json"
    index["fold_jobs"] = [".."]
    index_path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="invalid relative path"):
        af3.collect_data(escaped)
    linked = data_workspace(tmp_path / "linked")
    payload = linked / af3.DATA_OUTPUT_DIR / af3.HANDOFF_DIR_NAME / "pdl1-binder" / "pdl1-binder_data.json"
    target = linked / "target.json"
    target.write_bytes(payload.read_bytes())
    payload.unlink()
    payload.symlink_to(target)
    with pytest.raises(ScientificAdapterError, match="must not use symlinks"):
        af3.collect_data(linked)
    with pytest.raises(ScientificAdapterError, match="unsupported"):
        af3.collect_stage_output("unknown-collector-v1", request(), tmp_path)


def test_result_collector_validates_the_upstream_layout_and_publishes_the_closure(tmp_path: Path) -> None:
    workspace = inference_workspace(tmp_path)
    first = af3.collect_stage_output(af3.RESULT_COLLECTOR_ID, request(), workspace)
    second = af3.collect_result(request(), workspace)
    assert first == second
    entries = {entry["name"]: entry for entry in first.manifest["entries"]}  # type: ignore[union-attr]
    assert {
        "inference-receipt",
        "pdl1-binder.model",
        "pdl1-binder.summary-confidences",
        "pdl1-binder.confidences",
        "pdl1-binder.ranking-scores",
        "pdl1-binder.seed-1_sample-0.model",
        "pdl1-binder.seed-1_sample-0.summary-confidences",
        "pdl1-binder.seed-1_sample-1.model",
        "pdl1-binder.seed-1_sample-1.summary-confidences",
        "semantic-validation",
    } == set(entries)
    assert entries["pdl1-binder.model"]["semantic_type"] == "protein-structure-mmcif/v1"  # type: ignore[index]
    assert entries["pdl1-binder.model"]["artifact"]["media_type"] == "chemical/x-mmcif"  # type: ignore[index]
    validation_blob = first.blobs[entries["semantic-validation"]["artifact"]["artifact_id"]]  # type: ignore[index]
    validation = json.loads(validation_blob)
    assert validation["validator_id"] == af3.VALIDATOR_ID
    assert validation["status"] == "passed"
    assert validation["fold_jobs"] == ["pdl1-binder"]
    assert validation["structure_count"] == 3
    assert validation["atom_count"] == 36
    ranking = first.blobs[entries["pdl1-binder.ranking-scores"]["artifact"]["artifact_id"]]  # type: ignore[index]
    assert ranking.decode().splitlines()[0] == "seed,sample,ranking_score"
    assert len(first.blobs) == len(entries)

    selected = af3.collect_result(request("positive-selected-job.json"), workspace)
    # The semantic-validation document binds the request digest, so only that
    # entry may differ between the two accepted requests.
    stable = lambda output: [  # noqa: E731 - local comparison helper
        entry for entry in output.manifest["entries"] if entry["name"] != "semantic-validation"
    ]
    assert stable(selected) == stable(first)


def test_result_collector_refuses_incomplete_or_out_of_range_outputs(tmp_path: Path) -> None:
    with pytest.raises(ScientificAdapterError, match="terminal PASS"):
        failed = inference_workspace(tmp_path / "failed")
        receipt = _receipt("inference", status="FAIL")
        (failed / af3.INFERENCE_RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")
        af3.collect_result(request(), failed)
    wrong_parameters = inference_workspace(tmp_path / "params")
    receipt = _receipt("inference")
    receipt["parameters"]["sha256"] = "0" * 64
    (wrong_parameters / af3.INFERENCE_RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="exact licensed parameters"):
        af3.collect_result(request(), wrong_parameters)
    out_of_range = inference_workspace(tmp_path / "range")
    summary = out_of_range / af3.INFERENCE_OUTPUT_DIR / "pdl1-binder" / "pdl1-binder_summary_confidences.json"
    summary.write_text(_summary(7.5), encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="ranking_score"):
        af3.collect_result(request(), out_of_range)
    empty_structure = inference_workspace(tmp_path / "empty")
    model = empty_structure / af3.INFERENCE_OUTPUT_DIR / "pdl1-binder" / "seed-1_sample-0" / "model.cif"
    model.write_bytes(b"data_prediction\n#\n")
    with pytest.raises(ScientificAdapterError, match="ATOM|degenerate|atom"):
        af3.collect_result(request(), empty_structure)
    no_samples = inference_workspace(tmp_path / "samples", samples=1)
    sample_dir = no_samples / af3.INFERENCE_OUTPUT_DIR / "pdl1-binder" / "seed-1_sample-0"
    for item in sample_dir.iterdir():
        item.unlink()
    sample_dir.rmdir()
    with pytest.raises(ScientificAdapterError, match="seed/sample"):
        af3.collect_result(request(), no_samples)
    other_job = inference_workspace(tmp_path / "other", jobs=("another-job",))
    with pytest.raises(ScientificAdapterError, match="selected fold job"):
        af3.collect_result(request("positive-selected-job.json"), other_job)


@pytest.mark.asyncio
async def test_controller_admits_the_bound_plan_and_renders_the_cpu_stage_first(tmp_path: Path) -> None:
    promoted = _promoted_profile()
    renderer = _renderer(tmp_path, promoted)
    catalog_profile = ScientificWorkloadProfile(MappingProxyType(promoted))
    access = ArtifactAccessContext(profile="academic", receipt_digest=digest("af3-access"), tenant_id="academic-poc")
    operation_id = uuid4()
    plan = renderer.plan(
        catalog_profile, request(), operation_id=operation_id, access_context=access, input_artifacts=()
    )
    localized = renderer.verify_runtime_artifacts(catalog_profile, plan, access)
    bound = renderer.bind_runtime_artifacts(catalog_profile, plan, access, localized)
    frozen = scheduling(bound, captured_at=NOW)
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="controller:af3",
        namespace=af3.EXECUTION_NAMESPACE,
        clock=lambda: NOW,
    )
    fold_input = request()["input_manifest"]
    manifest_id = uuid4()
    verified_manifest = VerifiedInputManifest(
        manifest_id="af3-inputs",
        manifest_artifact_id=manifest_id,
        manifest_digest=digest("af3-input-manifest"),
        entries=(
            ScientificInputArtifact(
                logical_artifact_id=fold_input["artifact_id"],
                semantic_type="alphafold3-fold-input/v1",
                artifact_id=uuid4(),
                digest=f"sha256:{fold_input['sha256']}",
                size_bytes=fold_input["size_bytes"],
                media_type=fold_input["media_type"],
            ),
        ),
    )
    admitted = await controller.admit(
        operation_id=operation_id,
        tenant_id="academic-poc",
        model_id=af3.MODEL_ID,
        variant_id=af3.VARIANT_ID,
        input_artifact_id=manifest_id,
        plan=bound.controller_plan,
        scheduling=frozen,
        execution_plan=bound,
        access_context=access,
        input_manifest=verified_manifest,
        runtime_artifacts=localized,
    )
    assert admitted.execution_plan == bound
    assert admitted.scheduling.stage(af3.DATA_STAGE_ID).resolved_local_queue == af3.DATA_LOCAL_QUEUE
    assert admitted.scheduling.stage(af3.INFERENCE_STAGE_ID).resolved_local_queue == af3.INFERENCE_LOCAL_QUEUE
    await controller.reconcile_once()
    workload = cluster.apply_history[0]
    assert workload.namespace == af3.EXECUTION_NAMESPACE
    assert workload.stage_id == af3.DATA_STAGE_ID
    assert workload.scheduling.resolved_cluster_queue == af3.DATA_CLUSTER_QUEUE
    assert workload.scheduling.accelerator_count == 0
    assert workload.invocation == bound.invocation(af3.DATA_STAGE_ID, "main")
    assert tuple(item.logical_artifact_id for item in workload.runtime_artifacts) == (af3.REFERENCE_ARTIFACT,)


def _promoted_profile() -> dict[str, Any]:
    """The fragment promoted to a runnable shape with synthetic receipts.

    Only the renderer's admission requires a runnable profile; the promotion
    changes no identity and proves the fragment needs no structural change.
    """

    promoted = copy.deepcopy(dict(profile()))
    promoted["state"] = "active"
    promoted["route_exposed"] = True
    promoted["source"]["classification"] = "qualified-input"
    promoted["execution_identity"]["artifact_manifest_digest"] = "7" * 64
    promoted["execution_identity"]["execution_identity_sha256"] = "8" * 64
    promoted["interface"]["mcp"]["invocable"] = True
    promoted["access"]["receipt_digest"] = sha("af3-deployment-authorization")
    promoted["semantic_validation"]["state"] = "active"
    promoted["qualification"] = {
        "h100_semantic_receipt_sha256": sha("af3-r6-h100-semantic"),
        "public_completion_receipt_sha256": None,
        "scheduler_eligibility_receipt_sha256": None,
        "execution_map_sha256": sha("af3-execution-map"),
        "qualified_at": "2026-09-04T00:00:00Z",
    }
    validator = Draft202012Validator(load(PROFILE_SCHEMA_PATH), format_checker=FormatChecker())
    assert list(validator.iter_errors(promoted)) == []
    return promoted


def _renderer(tmp_path: Path, promoted: dict[str, Any]) -> FileScientificManifestRenderer:
    fragment = execution_fragment()
    model = copy.deepcopy(fragment["model"])
    model["execution_identity_sha256"] = promoted["execution_identity"]["execution_identity_sha256"]
    path = tmp_path / "execution-map.json"
    path.write_text(json.dumps({"schema": fragment["execution_map_schema"], "models": [model]}), encoding="utf-8")
    catalog = ScientificProfileCatalog(
        profiles={af3.MODEL_ID: ScientificWorkloadProfile(MappingProxyType(promoted))},
        validators=ScientificProfileCatalog.load(CATALOG_ROOT)._validators,  # type: ignore[attr-defined]  # noqa: SLF001
    )
    return FileScientificManifestRenderer(
        path=path,
        profiles=catalog,
        tools_image="registry.test/control@sha256:" + "9" * 64,
        internal_api_url="http://control.fs2.svc:8080",
        capability_authority=ScientificWorkloadCapabilityAuthority(
            KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"k" * 32})
        ),
    )


def test_execution_map_fragment_is_consumed_by_the_current_renderer_end_to_end(tmp_path: Path) -> None:
    """The decisive offline proof: fragment profile + fragment map -> pods.

    The current controller parses the fragment's execution-map entry, compiles
    the request through the dispatcher, passes its AlphaFold 3 gate, binds the
    published reference receipt and the private parameter file, and renders a
    CPU pod on the reference plane and a GPU pod on the private claim.
    """

    promoted = _promoted_profile()
    renderer = _renderer(tmp_path, promoted)
    catalog_profile = ScientificWorkloadProfile(MappingProxyType(promoted))
    access = ArtifactAccessContext(profile="academic", receipt_digest=digest("af3-access"), tenant_id="academic-poc")
    plan = renderer.plan(catalog_profile, request(), operation_id=uuid4(), access_context=access, input_artifacts=())
    assert plan.execution_map_sha256 == renderer.execution_map_sha256
    localized = renderer.verify_runtime_artifacts(catalog_profile, plan, access)
    assert {item.logical_artifact_id for item in localized} == {af3.PARAMETERS_ARTIFACT, af3.REFERENCE_ARTIFACT}
    databases = next(item for item in localized if item.logical_artifact_id == af3.REFERENCE_ARTIFACT)
    assert databases.localization_receipt_digest == f"sha256:{af3.REFERENCE_RECEIPT_SHA256}"
    assert databases.aggregate_tree is not None
    assert databases.aggregate_tree.canonical_path == af3.REFERENCE_DATASET_SUB_PATH
    bound = renderer.bind_runtime_artifacts(catalog_profile, plan, access, localized)
    bound.assert_controller_bound()
    data = bound.invocation(af3.DATA_STAGE_ID, "main")
    inference = bound.invocation(af3.INFERENCE_STAGE_ID, "main")
    assert data.runtime_mounts[0].readiness_receipt_sha256 == af3.REFERENCE_RECEIPT_SHA256
    assert data.runtime_mounts[0].expected_manifest_sha256 == af3.REFERENCE_MANIFEST_SHA256
    assert inference.runtime_mounts[0].expected_content_sha256 == af3.PARAMETERS_SHA256
    assert inference.runtime_mounts[0].sub_path == af3.PARAMETERS_FILENAME

    snapshot = scheduling(bound, captured_at=NOW)
    raw_materialization = ResolvedArtifactMaterialization.resolve(
        data.materializations[0],
        artifact_id=uuid4(),
        digest=digest("raw-af3-request"),
        size_bytes=867,
        media_type="application/json",
        compression=None,
    )
    common = {
        "operation_id": uuid4(),
        "batch_id": uuid4(),
        "workload_id": uuid4(),
        "attempt_number": 1,
        "tenant_id": "academic-poc",
        "model_id": af3.MODEL_ID,
        "variant_id": af3.VARIANT_ID,
        "input_artifact_id": uuid4(),
        "service_class": ServiceClass.CUSTOMER_BATCH,
        "scheduling_snapshot_digest": snapshot.digest,
        "namespace": af3.EXECUTION_NAMESPACE,
        "route_namespace": af3.EXECUTION_NAMESPACE,
        "kind": WorkloadKind.JOB,
        "access_context": access,
        "execution_map_sha256": bound.execution_map_sha256,
    }
    cpu_resource = WorkloadResource(
        **common,
        attempt_id=uuid4(),
        stage_id=af3.DATA_STAGE_ID,
        shard_id="main",
        name="af3-data-pipeline",
        scheduling=snapshot.stage(af3.DATA_STAGE_ID),
        invocation=data,
        materializations=(raw_materialization,),
        runtime_artifacts=tuple(item for item in localized if item.logical_artifact_id == af3.REFERENCE_ARTIFACT),
        execution_binding=bound.execution_binding(af3.DATA_STAGE_ID),
    )
    gpu_resource = WorkloadResource(
        **common,
        attempt_id=uuid4(),
        stage_id=af3.INFERENCE_STAGE_ID,
        shard_id="main",
        name="af3-inference",
        scheduling=snapshot.stage(af3.INFERENCE_STAGE_ID),
        invocation=inference,
        materializations=(
            ResolvedArtifactMaterialization.resolve(
                inference.materializations[0],
                artifact_id=uuid4(),
                digest=digest("processed-input"),
                size_bytes=10,
                media_type="application/x-tar",
                compression=None,
            ),
        ),
        runtime_artifacts=tuple(item for item in localized if item.logical_artifact_id == af3.PARAMETERS_ARTIFACT),
        execution_binding=bound.execution_binding(af3.INFERENCE_STAGE_ID),
    )
    cpu_pod = renderer.render(cpu_resource)["spec"]["template"]["spec"]  # type: ignore[index]
    assert cpu_pod["serviceAccountName"] == af3.SERVICE_ACCOUNT_NAME
    assert cpu_pod["nodeSelector"] == {"storage.fs2.nebius/reference-data": "true"}
    assert cpu_pod["securityContext"]["supplementalGroups"] == [af3.REFERENCE_DATA_GID]
    assert "fsGroup" not in cpu_pod["securityContext"]
    cpu_volumes = {item["name"]: item for item in cpu_pod["volumes"]}
    assert cpu_volumes["alphafold3-databases"]["hostPath"] == {"path": af3.REFERENCE_HOST_ROOT, "type": "Directory"}
    cpu_mounts = {item["name"]: item for item in cpu_pod["containers"][0]["volumeMounts"]}
    assert cpu_mounts["alphafold3-databases"]["mountPath"] == af3.REFERENCE_MOUNT_PATH
    assert "subPath" not in cpu_mounts["alphafold3-databases"]
    assert not any("persistentVolumeClaim" in item for item in cpu_pod["volumes"])
    assert cpu_pod["containers"][0]["resources"]["requests"] == {
        "cpu": af3.DATA_CPU,
        "memory": af3.DATA_MEMORY,
        "ephemeral-storage": af3.DATA_EPHEMERAL_STORAGE,
    }
    command = cpu_pod["containers"][0]["command"]
    assert command[:3] == [*af3.RUNTIME_COMMAND, "data"]
    assert command[command.index("--threads") + 1] == af3.DATA_CPU
    assert command[command.index("--reference-receipt") + 1] == af3.REFERENCE_RECEIPT_PATH

    gpu_pod = renderer.render(gpu_resource)["spec"]["template"]["spec"]  # type: ignore[index]
    assert gpu_pod["serviceAccountName"] == af3.SERVICE_ACCOUNT_NAME
    assert gpu_pod["securityContext"]["supplementalGroups"] == [af3.PARAMETERS_SUPPLEMENTAL_GROUP]
    assert "fsGroup" not in gpu_pod["securityContext"]
    gpu_volumes = {item["name"]: item for item in gpu_pod["volumes"]}
    assert gpu_volumes["alphafold3-parameters"]["persistentVolumeClaim"] == {
        "claimName": af3.PARAMETERS_CLAIM,
        "readOnly": True,
    }
    assert not any("hostPath" in item for item in gpu_pod["volumes"])
    gpu_mounts = {item["name"]: item for item in gpu_pod["containers"][0]["volumeMounts"]}
    assert gpu_mounts["alphafold3-parameters"]["mountPath"] == af3.PARAMETERS_MOUNT_PATH
    assert gpu_mounts["alphafold3-parameters"]["subPath"] == af3.PARAMETERS_SOURCE_SUB_PATH
    assert not any(item["mountPath"] == af3.REFERENCE_MOUNT_PATH for item in gpu_mounts.values())
    gpu_command = gpu_pod["containers"][0]["command"]
    assert gpu_command[:3] == [*af3.RUNTIME_COMMAND, "inference"]
    environment = {item["name"]: item.get("value") for item in gpu_pod["containers"][0]["env"]}
    assert environment["FS2_AF3_PARAMETER_PATH"] == af3.PARAMETERS_MOUNT_PATH
    assert environment["FS2_NETWORK_MODE"] == "offline"

    with pytest.raises(ScientificExecutionMapError, match="immutable execution-map route"):
        renderer.render(replace(gpu_resource, namespace="fs2-models", route_namespace="fs2-models"))


def test_execution_map_fragment_refuses_a_drifted_reference_receipt(tmp_path: Path) -> None:
    promoted = _promoted_profile()
    fragment = execution_fragment()
    model = copy.deepcopy(fragment["model"])
    model["execution_identity_sha256"] = promoted["execution_identity"]["execution_identity_sha256"]
    databases = next(item for item in model["runtime_artifacts"] if item["artifact_id"] == af3.REFERENCE_ARTIFACT)
    databases["verification_receipt"]["content"]["manifest_sha256"] = "1" * 64
    path = tmp_path / "drifted-map.json"
    path.write_text(json.dumps({"schema": fragment["execution_map_schema"], "models": [model]}), encoding="utf-8")
    catalog = ScientificProfileCatalog(
        profiles={af3.MODEL_ID: ScientificWorkloadProfile(MappingProxyType(promoted))},
        validators=ScientificProfileCatalog.load(CATALOG_ROOT)._validators,  # type: ignore[attr-defined]  # noqa: SLF001
    )
    with pytest.raises((ScientificExecutionMapError, ValueError)):
        FileScientificManifestRenderer(path=path, profiles=catalog)


def test_activation_gates_name_the_exact_blockers_without_claiming_readiness() -> None:
    fragment = execution_fragment()
    assert fragment["state"] == "identities-complete-pending-serialized-integration"
    gates = fragment["activation_gates"]
    for marker in (
        "reference-cpu-class",
        "allow-list",
        "academic-claim-ownership",
        "formal-license",
        "cpu-class-record",
    ):
        assert any(gate.startswith(f"{marker}:") for gate in gates), marker
    reference = load(IMAGE_ROOT / "contracts/af3-reference-data-binding.json")
    assert reference["state"] == "pending-publication", (
        "the image contract predates the receipt; binding it is integration work"
    )
    candidate = profile()
    assert candidate["route_exposed"] is False
    assert any("route stays closed" in item for item in candidate["policy"]["limitations"])  # type: ignore[index]
    recipe = load(ACTIVATION_ROOT / "integration-recipe.json")
    assert recipe["shared_edits"][0]["file"].endswith("scientific_batch/adapters/__init__.py")
    assert af3.MODEL_ID not in json.dumps(load(CATALOG_ROOT / "contracts/scientific-workload-profiles.json"))
