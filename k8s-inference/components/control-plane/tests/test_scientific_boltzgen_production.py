"""Production-path regressions for the staged BoltzGen adapter."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import zstandard
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository

from fs2_serve.crypto import KeyedHasher
from fs2_serve.scientific_batch import (
    MaterializationMode,
    ScientificAdapterError,
    ScientificInputArtifact,
    VerifiedInputManifest,
    companion,
)
from fs2_serve.scientific_batch.adapters import CollectionPendingError, boltzgen, collect_stage_output
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, _invocation_json
from fs2_serve.scientific_batch.models import AdapterExecutionPlan, StageInvocation
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog
from fs2_serve.scientific_batch.scheduling import SchedulingContractResolver

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = SOLUTION_ROOT / "catalog/runtime"
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters/boltzgen"


def _request() -> dict[str, object]:
    value = json.loads((ADAPTER_ROOT / "fixtures/positive-design.json").read_text(encoding="utf-8"))
    value["input_manifest"] = {
        "artifact_id": "00000000-0000-0000-0000-000000000002",
        "sha256": "d" * 64,
        "size_bytes": 512,
        "media_type": "application/vnd.fs2.scientific-manifest+json",
        "compression": "none",
    }
    return value


def _input() -> ScientificInputArtifact:
    return ScientificInputArtifact(
        logical_artifact_id=boltzgen.CAMPAIGN_INPUT_ID,
        semantic_type=boltzgen.CAMPAIGN_INPUT_SEMANTIC_TYPE,
        artifact_id=UUID("00000000-0000-0000-0000-000000000001"),
        digest="sha256:" + "c" * 64,
        size_bytes=1024,
        media_type=boltzgen.CAMPAIGN_INPUT_MEDIA_TYPE,
        compression="gzip",
    )


def _renderer(catalog: ScientificProfileCatalog) -> FileScientificManifestRenderer:
    return FileScientificManifestRenderer(
        path=CATALOG_ROOT / "contracts/scientific-execution-map.json",
        profiles=catalog,
        tools_image="registry.test/fs2-control@sha256:" + "9" * 64,
        internal_api_url="http://fs2-control.fs2-system.svc:8080",
        capability_authority=ScientificWorkloadCapabilityAuthority(
            KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"b" * 32})
        ),
    )


def _bound_plan() -> tuple[ScientificProfileCatalog, FileScientificManifestRenderer, AdapterExecutionPlan]:
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    renderer = _renderer(catalog)
    profile = catalog.get(boltzgen.MODEL_ID)
    access = renderer.access_context(profile, tenant_id="boltz-production-test")
    plan = renderer.plan(
        profile,
        _request(),
        operation_id=UUID("10000000-0000-4000-8000-000000000001"),
        access_context=access,
        input_artifacts=(_input(),),
    )
    localizations = renderer.verify_runtime_artifacts(profile, plan, access)
    return catalog, renderer, renderer.bind_runtime_artifacts(profile, plan, access, localizations)


def _scheduling() -> SchedulingContractResolver:
    return SchedulingContractResolver(
        {
            "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
            "pool_node_label_key": "accelerator.fs2.nebius/pool-id",
            "service_classes": {
                "customer-batch": {
                    "workload_priority_class": "customer-batch",
                    "priority": 0,
                    "default_local_queue": "scientific",
                    "preemption_mode": "restartable",
                    "pool_preference": ["h100-reserved-8x", "h100-1x"],
                    "max_queue_seconds": 900,
                    "max_execution_seconds": 86400,
                    "caller_selectable": True,
                }
            },
            "local_queues": {
                "scientific": {
                    "metadata": {"name": "scientific", "namespace": "fs2-models"},
                    "spec": {"clusterQueue": "inference"},
                },
                "general-cpu": {
                    "metadata": {"name": "general-cpu", "namespace": "fs2-models"},
                    "spec": {"clusterQueue": "general-cpu"},
                },
            },
            "cluster_queues": {
                "inference": {"metadata": {"name": "inference"}, "spec": {}},
                "general-cpu": {"metadata": {"name": "general-cpu"}, "spec": {}},
            },
            "workload_priority_classes": {"customer-batch": {"value": 0}},
            "local_queue_routes": {
                "scientific": {
                    "namespace": "fs2-models",
                    "cluster_queue": "inference",
                    "model_ids": [],
                    "tenant_ids": [],
                    "service_classes": [],
                },
                "general-cpu": {
                    "namespace": "fs2-models",
                    "cluster_queue": "general-cpu",
                    "model_ids": [],
                    "tenant_ids": [],
                    "service_classes": [],
                },
            },
            "model_eligible_pool_ids": {boltzgen.MODEL_ID: ["h100-1x", "h100-reserved-8x"]},
            "cpu_classes_schema": "fs2-serve.nebius.ai/cpu-stage-classes/v1",
            "cpu_classes": {
                "general-cpu": {
                    "local_queue": "general-cpu",
                    "cluster_queue": "general-cpu",
                    "namespace": "fs2-models",
                    "resource_flavor": "general-cpu",
                    "pool_resolution": {"mode": "per-pool-flavor", "pool_id": "general-cpu-8x"},
                    "node_selector": {
                        "workload.fs2.nebius/general-cpu": "true",
                        "capacity.fs2.nebius/pool-id": "general-cpu-8x",
                    },
                    "tolerations": [
                        {
                            "key": "workload.fs2.nebius/general-cpu",
                            "operator": "Equal",
                            "value": "true",
                            "effect": "NoSchedule",
                        }
                    ],
                    "eligible_pool_ids": ["general-cpu-8x"],
                    "schedulable_capacity": {
                        "cpu_millicores": 8000,
                        "memory_mib": 32768,
                        "ephemeral_storage_mib": 131072,
                    },
                }
            },
            "cpu_stage_requests": {"general-cpu": {"cpu_millicores": 4000, "memory_mib": 16384}},
            "namespace_bound_models": {},
            "pools": {
                "h100-1x": {
                    "resource_flavor": "h100-1x",
                    "accelerator_resource_name": "nvidia.com/gpu",
                    "capacity": 1,
                },
                "h100-reserved-8x": {
                    "resource_flavor": "h100-reserved-8x",
                    "accelerator_resource_name": "nvidia.com/gpu",
                    "capacity": 8,
                },
            },
        }
    )


def _runtime_marker(invocation: StageInvocation) -> str:
    return json.dumps(
        {
            "schema": companion.RUNTIME_LOCALIZATION_SCHEMA,
            "operation_id": str(uuid4()),
            "attempt_id": str(uuid4()),
            "tenant_id": "boltz-production-test",
            "model_id": boltzgen.MODEL_ID,
            "variant_id": boltzgen.VARIANT_ID,
            "stage_id": invocation.stage_id,
            "artifacts": [],
        }
    )


def _completion(invocation: StageInvocation, **overrides: object) -> bytes:
    command = invocation.argv[3:]
    value: dict[str, object] = {
        "schema": boltzgen.STAGE_COMPLETION_SCHEMA,
        "status": "passed",
        "stage_id": invocation.stage_id,
        "shard_id": invocation.shard_id,
        "logical_output_id": invocation.produces,
        "collector_id": invocation.collector_id,
        "validator_id": invocation.validator_id,
        "argv_sha256": hashlib.sha256(
            json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
    }
    value.update(overrides)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _publish_completion(invocation: StageInvocation, workspace: Path, **overrides: object) -> None:
    marker = workspace / boltzgen.STAGE_COMPLETION_RELATIVE_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(_completion(invocation, **overrides))


def _mmcif() -> bytes:
    return b"""data_design
#
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_asym_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
ATOM 1 N N A 11.104 13.207 14.099
ATOM 2 C CA A 12.104 13.207 14.099
ATOM 3 N N B 21.104 23.207 24.099
ATOM 4 C CA B 22.104 23.207 24.099
#
"""


class _ArtifactClient:
    def __init__(self) -> None:
        self.uploads: dict[str, tuple[bytes, dict[str, object]]] = {}
        self.downloads: dict[UUID, tuple[bytes, str]] = {}

    def upload(self, *, identity: str, content: bytes, media_type: str, compression: str | None):
        reference: dict[str, object] = {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": media_type,
        }
        if compression is not None:
            reference["compression"] = compression
        self.uploads[identity] = (content, reference)
        return reference

    def download(
        self,
        artifact_id: UUID,
        *,
        expected_digest: str,
        expected_size_bytes: int,
        expected_media_type: str,
    ) -> bytes:
        content, media_type = self.downloads[artifact_id]
        assert expected_digest == "sha256:" + hashlib.sha256(content).hexdigest()
        assert expected_size_bytes == len(content)
        assert expected_media_type == media_type
        return content


@pytest.mark.asyncio
async def test_public_request_freezes_schedules_reconciles_and_renders_a_fully_bound_plan() -> None:
    catalog, renderer, plan = _bound_plan()
    profile = catalog.get(boltzgen.MODEL_ID)
    access = renderer.access_context(profile, tenant_id="boltz-production-test")
    localizations = renderer.verify_runtime_artifacts(profile, plan, access)
    plan.assert_controller_bound()
    assert all(
        invocation.collector_id == invocation.validator_id == "boltzgen-v0-3-2" for invocation in plan.invocations
    )
    assert all(
        invocation.handoff_name == boltzgen.STAGE_HANDOFF_NAME
        for invocation in plan.invocations
        if invocation.stage_id != "filtering"
    )

    scheduling = _scheduling().freeze(
        service_class="customer-batch",
        model_id=boltzgen.MODEL_ID,
        tenant_id="boltz-production-test",
        profile=profile.value,
        plan=plan.controller_plan,
        workload_namespace="fs2-models",
    )
    assert {item.resource_class.value for item in scheduling.stages} == {"cpu", "gpu"}
    repository = FakeScientificBatchRepository()
    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=repository,
        cluster=cluster,
        controller_id="boltz-production-controller",
        namespace="fs2-models",
    )
    operation_id = UUID("10000000-0000-4000-8000-000000000001")
    manifest_id = UUID("00000000-0000-0000-0000-000000000002")
    await controller.admit(
        operation_id=operation_id,
        tenant_id="boltz-production-test",
        model_id=boltzgen.MODEL_ID,
        variant_id=boltzgen.VARIANT_ID,
        input_artifact_id=manifest_id,
        plan=plan.controller_plan,
        scheduling=scheduling,
        execution_plan=plan,
        access_context=access,
        input_manifest=VerifiedInputManifest(
            manifest_id="boltz-production-inputs",
            manifest_artifact_id=manifest_id,
            manifest_digest="sha256:" + "d" * 64,
            entries=(_input(),),
        ),
        runtime_artifacts=localizations,
    )
    assert await controller.reconcile_once() == operation_id
    resource = cluster.apply_history[0]
    rendered = renderer.render(resource)
    pod = rendered["spec"]["template"]["spec"]
    model = pod["containers"][0]
    assert model["command"] == list(resource.invocation.argv)
    assert model["command"][:3] == [
        "python",
        f"{resource.invocation.working_directory}/.fs2/stage-runner.py",
        "--",
    ]
    environment = {item["name"]: item["value"] for item in model["env"]}
    assert environment["FS2_STAGE_ID"] == resource.stage_id
    assert environment["FS2_SHARD_ID"] == resource.shard_id
    assert environment["FS2_LOGICAL_OUTPUT_ID"] == resource.invocation.produces
    assert environment["FS2_COLLECTOR_ID"] == "boltzgen-v0-3-2"
    assert environment["FS2_VALIDATOR_ID"] == "boltzgen-v0-3-2"
    assert pod["initContainers"][0]["command"][1] == "scientific-prepare-workspace"


def test_atomic_runner_has_no_early_or_failed_completion_and_mismatch_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _catalog, _renderer_value, plan = _bound_plan()
    template = plan.invocations[0]
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)

    workspace = root / "success"
    child = tmp_path / "child.py"
    child.write_text("from pathlib import Path\nPath('payload.bin').write_bytes(b'complete')\n", encoding="utf-8")
    invocation = replace(
        template,
        argv=(
            "python",
            f"{template.working_directory}/.fs2/stage-runner.py",
            "--",
            sys.executable,
            str(child),
        ),
    )
    companion.prepare_workspace(
        workspace,
        runtime_localization_json=_runtime_marker(invocation),
        stage_invocation_json=_invocation_json(invocation),
    )
    partial = workspace / ".fs2/.stage-complete.json.1.partial"
    partial.write_bytes(_completion(invocation))
    with pytest.raises(CollectionPendingError):
        collect_stage_output(invocation, workspace)

    environment = {
        **os.environ,
        "FS2_STAGE_ID": invocation.stage_id,
        "FS2_SHARD_ID": invocation.shard_id,
        "FS2_LOGICAL_OUTPUT_ID": invocation.produces,
        "FS2_COLLECTOR_ID": invocation.collector_id,
        "FS2_VALIDATOR_ID": invocation.validator_id,
    }
    completed = subprocess.run(  # noqa: S603 - every executable and argument is this test's fixture
        [sys.executable, workspace / companion.STAGE_RUNNER_RELATIVE_PATH, "--", *invocation.argv[3:]],
        cwd=workspace,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0
    assert (workspace / boltzgen.STAGE_COMPLETION_RELATIVE_PATH).read_bytes() == _completion(invocation)

    failed_workspace = root / "failed"
    failing_child = tmp_path / "failing.py"
    failing_child.write_text("raise SystemExit(17)\n", encoding="utf-8")
    failed = replace(invocation, argv=(*invocation.argv[:3], sys.executable, str(failing_child)))
    companion.prepare_workspace(
        failed_workspace,
        runtime_localization_json=_runtime_marker(failed),
        stage_invocation_json=_invocation_json(failed),
    )
    failed_run = subprocess.run(  # noqa: S603 - every executable and argument is this test's fixture
        [sys.executable, failed_workspace / companion.STAGE_RUNNER_RELATIVE_PATH, "--", *failed.argv[3:]],
        cwd=failed_workspace,
        env=environment,
        check=False,
    )
    assert failed_run.returncode == 17
    assert not (failed_workspace / boltzgen.STAGE_COMPLETION_RELATIVE_PATH).exists()
    with pytest.raises(CollectionPendingError):
        collect_stage_output(failed, failed_workspace)

    mismatch_workspace = root / "mismatch"
    mismatch_workspace.mkdir()
    _publish_completion(invocation, mismatch_workspace, stage_id="another-stage")
    with pytest.raises(ScientificAdapterError, match="differs from the frozen invocation"):
        collect_stage_output(invocation, mismatch_workspace)


def test_intermediate_handoff_round_trips_through_global_collector_and_materializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _catalog, _renderer_value, plan = _bound_plan()
    configure = next(item for item in plan.invocations if item.stage_id == "configure")
    design = next(
        item for item in plan.invocations if item.stage_id == "design" and item.shard_id == configure.shard_id
    )
    root = tmp_path / "scientific"
    workspace = root / "configure"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(companion, "_ROOT", root)
    (workspace / "inputs").mkdir()
    (workspace / "inputs/design.yaml").write_text("entities: []\n", encoding="utf-8")
    (workspace / "stage-output.bin").write_bytes(b"complete-stage-output")
    _publish_completion(configure, workspace)

    client = _ArtifactClient()
    companion.collect_and_commit(
        client=client,  # type: ignore[arg-type]
        collector_id=configure.collector_id,
        validator_id=configure.validator_id,
        invocation_json=_invocation_json(configure),
        workspace=workspace,
        catalog_dir=CATALOG_ROOT,
        collection_deadline_seconds=1,
        poll_seconds=0.01,
        max_artifacts=configure.max_output_artifacts,
        max_output_bytes=configure.max_output_bytes,
    )
    content, reference = client.uploads[f"{configure.produces}:{boltzgen.STAGE_HANDOFF_NAME}"]
    assert reference["media_type"] == boltzgen.STAGE_HANDOFF_MEDIA_TYPE
    assert reference["compression"] == "zstd"
    assert len(content) <= boltzgen.MAX_STAGE_HANDOFF_BYTES
    assert not any(b"stage-complete.json" in value[0] for value in client.uploads.values())

    artifact_id = UUID(str(reference["artifact_id"]))
    client.downloads[artifact_id] = (content, boltzgen.STAGE_HANDOFF_MEDIA_TYPE)
    destination = root / "design"
    companion.prepare_workspace(
        destination,
        runtime_localization_json=_runtime_marker(design),
        stage_invocation_json=_invocation_json(design),
    )
    companion.materialize_artifact(
        client=client,  # type: ignore[arg-type]
        artifact_id=artifact_id,
        destination=destination,
        mode=MaterializationMode.OVERLAY_TAR,
        compression="zstd",
        yaml_name=None,
        reuse_prefix=None,
        expected_digest="sha256:" + hashlib.sha256(content).hexdigest(),
        expected_size_bytes=len(content),
        expected_media_type=boltzgen.STAGE_HANDOFF_MEDIA_TYPE,
    )
    assert (destination / "inputs/design.yaml").read_text(encoding="utf-8") == "entities: []\n"
    assert (destination / "stage-output.bin").read_bytes() == b"complete-stage-output"
    assert (destination / companion.STAGE_RUNNER_RELATIVE_PATH).is_file()


def test_materializer_cannot_replace_the_injected_stage_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "scientific"
    root.mkdir()
    monkeypatch.setattr(companion, "_ROOT", root)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        payload = b"attacker-controlled runner"
        member = tarfile.TarInfo(".fs2/stage-runner.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    content = zstandard.ZstdCompressor().compress(raw.getvalue())
    artifact_id = uuid4()
    client = _ArtifactClient()
    client.downloads[artifact_id] = (content, boltzgen.STAGE_HANDOFF_MEDIA_TYPE)

    with pytest.raises(ValueError, match="reserved control namespace"):
        companion.materialize_artifact(
            client=client,  # type: ignore[arg-type]
            artifact_id=artifact_id,
            destination=root / "target",
            mode=MaterializationMode.OVERLAY_TAR,
            compression="zstd",
            yaml_name=None,
            reuse_prefix=None,
            expected_digest="sha256:" + hashlib.sha256(content).hexdigest(),
            expected_size_bytes=len(content),
            expected_media_type=boltzgen.STAGE_HANDOFF_MEDIA_TYPE,
        )


def test_filtering_collects_exact_csv_and_mmcif_outputs_from_global_registry(tmp_path: Path) -> None:
    _catalog, _renderer_value, plan = _bound_plan()
    invocation = next(item for item in plan.invocations if item.stage_id == "filtering")
    workspace = tmp_path / "filtering"
    structures = workspace / "final_ranked_designs/final_2_designs"
    structures.mkdir(parents=True)
    rows = ["id,file_name,designed_chain_sequence,design_to_target_iptm,designfolding-filter_rmsd"]
    for index in (1, 2):
        name = f"design-{index}.cif"
        (structures / name).write_bytes(_mmcif())
        rows.append(f"design-{index},{name},ACDEFGHIKLMNPQRSTVWY,0.75,1.1")
    (workspace / "final_ranked_designs/final_designs_metrics_2.csv").write_text(
        "\n".join(rows) + "\n", encoding="utf-8"
    )
    _publish_completion(invocation, workspace)

    output = collect_stage_output(invocation, workspace)
    assert [item.media_type for item in output.artifacts] == [
        boltzgen.FINAL_RANKING_MEDIA_TYPE,
        boltzgen.FINAL_STRUCTURE_MEDIA_TYPE,
        boltzgen.FINAL_STRUCTURE_MEDIA_TYPE,
    ]
    assert output.validation["status"] == "passed"
    assert output.validation["design_count"] == 2
    assert output.validation["atom_count"] == 8
