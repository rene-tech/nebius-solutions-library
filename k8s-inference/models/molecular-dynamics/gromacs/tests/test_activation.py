import copy
import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location(
    "gromacs_activation", HERE / "activation/prepare.py"
)
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


def inputs():
    live = json.loads(
        (ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text()
    )
    live["models"] = [
        row
        for row in live["models"]
        if row["model_id"] not in {"gromacs", "gromacs-mpi"}
    ]
    live["qualification_baselines"] = {
        key: ids
        for key, ids in live.get("qualification_baselines", {}).items()
        if not {"gromacs", "gromacs-mpi"}.intersection(ids)
    }
    candidate = json.loads((HERE / "activation/workload-profile.json").read_text())[
        "profile"
    ]
    scheduler = {
        "pools": {pool: {} for pool in candidate["resources"]["compatible_pool_ids"]},
        "model_eligible_pool_ids": {"rfdiffusion": ["h100-1x"]},
        "quotas": {"unchanged": True},
    }
    raw = activation.canonical(scheduler)
    values = {
        "scientificBatch": {
            "executionMap": live,
            "schedulingContractSha256": activation.hashlib.sha256(raw).hexdigest(),
            "schedulingContractConfigMapName": "existing-abc123",
            "schedulingContractNamespace": "fs2-models",
            "schedulingContractKey": "scheduling.json",
        }
    }
    values["scientificArtifacts"] = {"mediaTypes": ["application/json", "image/png"]}
    image = "registry.example/gromacs@sha256:" + "1" * 64
    evidence = {
        "runtime_image": image,
        "recorded_at": "2026-09-23T07:00:00Z",
        "tests": [{"fixture": True}],
    }
    return values, raw, candidate, image, evidence, "2" * 64


def test_addition_preserves_current_models_snapshots_and_quotas(tmp_path):
    args = inputs()
    before = copy.deepcopy(args)
    profile, row, overlay, cm = activation.prepare(*args)
    assert args == before
    live = args[0]["scientificBatch"]["executionMap"]
    desired = overlay["scientificBatch"]["executionMap"]
    assert live["models"] == desired["models"][:-1]
    assert live.get("snapshot_bundles") == desired.get("snapshot_bundles")
    assert json.loads(cm["data"]["scheduling.json"])["quotas"] == {"unchanged": True}
    assert profile["qualification"]["public_completion_receipt_sha256"] is None
    assert set(overlay["scientificArtifacts"]["mediaTypes"]) == {
        "application/json",
        "image/png",
        "application/x-tar",
        "application/vnd.fs2.gromacs-checkpoint+json",
    }
    assert row["stages"][0]["required_node_labels"] == {"kubernetes.io/arch": "amd64"}
    assert row["runtime_artifacts"] == []
    Draft202012Validator(
        json.loads(
            (
                ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json"
            ).read_text()
        )
    ).validate(profile)

    activation.source_recipe(ROOT)
    from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
    from fs2_serve.scientific_batch.profile_catalog import (
        ScientificProfileCatalog,
        ScientificWorkloadProfile,
    )

    existing = ScientificProfileCatalog.load(ROOT / "catalog/runtime")
    profiles = ScientificProfileCatalog(
        profiles={
            **{p.model_id: p for p in existing.list() if p.model_id != "gromacs-mpi"},
            "gromacs": ScientificWorkloadProfile(profile),
        },
        validators=existing._validators,
    )
    path = tmp_path / "execution.json"
    path.write_bytes(activation.canonical(desired))
    renderer = FileScientificManifestRenderer(path=path, profiles=profiles)
    assert renderer.variant_id("gromacs") == "nvidia-2026-2-single-gpu-v1"
    assert renderer.collector_id("gromacs", "workflow") == "gromacs-workflow-v1"


def test_old_scheduler_or_wrong_worker_evidence_does_not_activate():
    args = list(inputs())
    args[1] += b" "
    with pytest.raises(ValueError, match="scheduler bytes"):
        activation.prepare(*args)
    args = list(inputs())
    args[4]["runtime_image"] = "wrong"
    with pytest.raises(ValueError, match="evidence"):
        activation.prepare(*args)


def test_source_recipe_binds_storage_and_native_worker():
    paths = {entry["path"] for entry in activation.source_recipe(ROOT)["files"]}
    assert (
        "components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage_routes.py"
        in paths
    )
    assert "models/molecular-dynamics/gromacs/runtime/fs2_gromacs/worker.py" in paths


def test_paired_successor_requalifies_both_apps_without_stale_intermediate_proof(monkeypatch):
    pair_spec = importlib.util.spec_from_file_location("gromacs_pair", HERE / "activation/prepare_pair.py")
    pair = importlib.util.module_from_spec(pair_spec)
    pair_spec.loader.exec_module(pair)
    monkeypatch.setitem(sys.modules, "prepare", activation)
    values, raw, _, _, _, _ = inputs()
    current = json.loads((ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text())
    values["scientificBatch"]["executionMap"] = current
    before_mpi = [row for row in current["models"] if row["model_id"] != "gromacs-mpi"]
    stale = activation.digest({"schema": current["schema"], "models": before_mpi})
    current["qualification_baselines"][stale] = [row["model_id"] for row in before_mpi]
    original = copy.deepcopy(values)
    candidates = {model: json.loads((HERE / "activation" / file).read_text())["profile"]
                  for model, file in [("gromacs", "workload-profile.json"),
                                      ("gromacs-mpi", "mpi-workload-profile.json")]}
    images = {model: "registry.example/gromacs@sha256:" + "9" * 64 for model in candidates}
    evidence = {model: {"runtime_image": image, "recorded_at": "2026-09-23T12:00:00Z", "tests": [{"fixture": True}]}
                for model, image in images.items()}
    profiles, overlay, _ = pair.replace_pair(values, raw, candidates, images, evidence, "a" * 64)
    assert values == original
    final = overlay["scientificBatch"]["executionMap"]
    assert stale not in final["qualification_baselines"]
    assert [row for row in final["models"] if row["model_id"] not in candidates] == [
        row for row in current["models"] if row["model_id"] not in candidates]
    expected = activation.digest({"schema": final["schema"], "models": final["models"]})
    assert {profile["qualification"]["execution_map_sha256"] for profile in profiles.values()} == {expected}


def test_mpi_addition_renders_the_full_frozen_gang(tmp_path):
    from fs2_serve.crypto import KeyedHasher
    from fs2_serve.scientific_batch.capability import (
        ScientificWorkloadCapabilityAuthority,
    )
    from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
    from fs2_serve.scientific_batch.profile_catalog import (
        ScientificProfileCatalog,
        ScientificWorkloadProfile,
    )
    from fs2_serve.scientific_batch.models import (
        ArtifactAccessContext,
        ResolvedArtifactMaterialization,
        ScientificInputArtifact,
        ServiceClass,
        StageSchedulingDecision,
        WorkloadKind,
        WorkloadResource,
    )

    args = list(inputs())
    args[2] = json.loads((HERE / "activation/mpi-workload-profile.json").read_text())[
        "profile"
    ]
    # MPI is additive to the full live baseline, including single-GPU GROMACS.
    args[0]["scientificBatch"]["executionMap"] = json.loads(
        (ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text()
    )
    baseline = args[0]["scientificBatch"]["executionMap"]
    baseline["models"] = [
        row for row in baseline["models"] if row["model_id"] != "gromacs-mpi"
    ]
    baseline["qualification_baselines"] = {
        key: ids
        for key, ids in baseline.get("qualification_baselines", {}).items()
        if "gromacs-mpi" not in ids
    }
    profile, row, overlay, _ = activation.prepare(*args)
    assert (
        overlay["scientificBatch"]["executionMap"]["models"][:-1]
        == args[0]["scientificBatch"]["executionMap"]["models"]
    )
    path = tmp_path / "mpi-execution.json"
    path.write_bytes(activation.canonical(overlay["scientificBatch"]["executionMap"]))
    existing = ScientificProfileCatalog.load(ROOT / "catalog/runtime")
    typed_profile = ScientificWorkloadProfile(profile)
    profiles = ScientificProfileCatalog(
        profiles={
            **{p.model_id: p for p in existing.list()},
            "gromacs-mpi": typed_profile,
        },
        validators=existing._validators,
    )
    renderer = FileScientificManifestRenderer(
        path=path,
        profiles=profiles,
        tools_image="registry.test/control@sha256:" + "9" * 64,
        internal_api_url="http://control.fs2.svc:8080",
        capability_authority=ScientificWorkloadCapabilityAuthority(
            KeyedHasher(active_key_id="test", keys={"test": b"k" * 32})
        ),
    )
    operation = uuid4()
    source = ScientificInputArtifact(
        "gromacs-inputs",
        "gromacs-input-bundle/v1",
        uuid4(),
        "sha256:" + "b" * 64,
        1000,
        "application/x-tar",
        "gzip",
    )
    body = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "run-workflow",
        "service_class": "customer-batch",
        "input_manifest": {
            "artifact_id": str(uuid4()),
            "sha256": "a" * 64,
            "size_bytes": 1000,
            "media_type": "application/vnd.fs2.scientific-manifest+json",
            "compression": "none",
        },
        "parameters": {
            "schema": "fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1",
            "nodes": 2,
            "jobs": [
                {
                    "id": "gang",
                    "steps": [
                        {"id": "md", "command": "mdrun", "args": ["-s", "md.tpr"]}
                    ],
                }
            ],
        },
    }
    access = ArtifactAccessContext(
        profile="public", receipt_digest=None, tenant_id="tenant-a"
    )
    plan = renderer.plan(
        typed_profile,
        body,
        operation_id=operation,
        access_context=access,
        input_artifacts=(source,),
    )
    invocation = plan.invocation("workflow", "gang")
    stage = plan.controller_plan.stages[0]
    decision = StageSchedulingDecision(
        "workflow",
        stage.resource_class,
        "inference",
        "scientific",
        "customer-batch",
        10,
        ("h100-ondemand-1x",),
        "nvidia.com/gpu",
        1,
        600,
        3600,
        stage.checkpoint_mode,
        stage.preemption_mode,
    )
    materialization = ResolvedArtifactMaterialization.resolve(
        invocation.materializations[0],
        artifact_id=source.artifact_id,
        digest=source.digest,
        size_bytes=source.size_bytes,
        media_type=source.media_type,
        compression=source.compression,
    )
    resource = WorkloadResource(
        operation_id=operation,
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id="workflow",
        shard_id=None,
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="gromacs-mpi",
        variant_id=row["variant_id"],
        input_artifact_id=uuid4(),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest="sha256:" + "a" * 64,
        namespace="fs2-models",
        name="gromacs-mpi-test",
        kind=WorkloadKind.JOB_SET,
        gang_size=2,
        scheduling=decision,
        invocation=invocation,
        materializations=(materialization,),
        access_context=access,
        execution_map_sha256=plan.execution_map_sha256,
        execution_binding=plan.execution_binding("workflow"),
    )
    manifest = renderer.render(resource)
    spec = manifest["spec"]
    assert spec["network"] == {
        "enableDNSHostnames": True,
        "publishNotReadyAddresses": True,
    }
    gang = spec["replicatedJobs"][0]
    assert gang["replicas"] == 2
    pod = gang["template"]["spec"]["template"]["spec"]
    runtime, collector = pod["containers"]
    env = {item["name"]: item for item in runtime["env"]}
    assert (
        env["FS2_MPI_HOSTS"]["value"]
        == "gromacs-mpi-test-gang-0-0.gromacs-mpi-test,gromacs-mpi-test-gang-1-0.gromacs-mpi-test"
    )
    assert len(env["FS2_MPI_SSH_SEED"]["value"]) == 64
    assert runtime["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert "--peer-collector" in collector["command"]
    assert "podAntiAffinity" in pod["affinity"]


def test_explicit_successor_changes_only_gromacs_image_and_identity():
    args = list(inputs())
    _, old_row, overlay, cm = activation.prepare(*args)
    args[0]["scientificBatch"].update(overlay["scientificBatch"])
    args[1] = cm["data"]["scheduling.json"].encode()
    before = copy.deepcopy(args[0])
    args[3] = "registry.example/gromacs@sha256:" + "3" * 64
    args[4]["runtime_image"] = args[3]
    args[5] = "4" * 64
    with pytest.raises(ValueError, match="explicit successor"):
        activation.prepare(*args)
    _, new_row, desired, new_cm = activation.prepare(*args, replace_existing=True)
    expected = copy.deepcopy(old_row)
    expected["stages"][0]["image"] = args[3]
    expected["execution_identity_sha256"] = new_row["execution_identity_sha256"]
    assert new_row == expected
    assert old_row["execution_identity_sha256"] != new_row["execution_identity_sha256"]
    assert new_cm == cm
    for key in ("snapshot_bundles", "qualification_baselines"):
        assert desired["scientificBatch"]["executionMap"].get(key) == before[
            "scientificBatch"
        ]["executionMap"].get(key)
    assert [
        row
        for row in desired["scientificBatch"]["executionMap"]["models"]
        if row["model_id"] != "gromacs"
    ] == [
        row
        for row in before["scientificBatch"]["executionMap"]["models"]
        if row["model_id"] != "gromacs"
    ]
