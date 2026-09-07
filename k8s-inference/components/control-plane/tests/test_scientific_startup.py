from __future__ import annotations

import copy
import importlib
import json
from dataclasses import replace
from types import MappingProxyType
from uuid import uuid4

import pytest
from conftest import CATALOG_ROOT, SOLUTION_ROOT
from test_scientific_batch_execution_handoff import (
    runtime_execution_map,
    runtime_plan,
    runtime_profile,
    scheduling,
)

from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.models import (
    ArtifactAccessContext,
    ResolvedArtifactMaterialization,
    ScientificBatchState,
    ScientificInputArtifact,
    ServiceClass,
    VerifiedInputManifest,
    WorkloadKind,
    WorkloadResource,
)
from fs2_serve.scientific_batch.profile_catalog import (
    ScientificProfileCatalog,
    ScientificProfileError,
    ScientificWorkloadProfile,
)
from fs2_serve.scientific_batch.startup import (
    StageStartupPolicy,
    apply_startup_policy,
    select_startup_policy,
    validate_bundle,
)


@pytest.fixture
def bundle():
    return json.loads((SOLUTION_ROOT / "acceptance/h100-fleet/snapshots/protenix-v2-bundle.json").read_text())


def choose(bundle):
    return select_startup_policy(
        {"backend": "cuda-criu", "bundle_id": bundle["bundle_id"]},
        {bundle["bundle_id"]: bundle},
        model_id=bundle["model_id"],
        stage_id=bundle["stage_id"],
        model_revision=bundle["profile_model_revision"],
        runtime_image=bundle["runtime_image"],
    )


def test_measured_bundle_and_frozen_policy_roundtrip(bundle):
    assert validate_bundle(bundle, bundle["bundle_id"])["qualified"]
    policy = choose(bundle)
    assert StageStartupPolicy.from_value(policy.to_value()) == policy
    bundle["pvc"] = "changed-after-admission"
    assert json.loads(policy.bundle_json)["pvc"] != bundle["pvc"]
    assert policy.to_value()["bundle"]["compatibility"]["compute_capability"] == "9.0"


@pytest.mark.parametrize(
    "field,value",
    [
        ("qualified", False),
        ("model_id", "mosaic"),
        ("stage_id", "prepare"),
        ("runtime_image", "runtime:mutable"),
        ("manifest_sha256", None),
        ("compatibility", {}),
    ],
)
def test_unqualified_or_different_bundle_is_not_selectable(bundle, field, value):
    bundle[field] = value
    with pytest.raises(ValueError):
        choose(bundle)


def test_default_normal_load_is_identity_transform():
    pod = {"metadata": {}, "spec": {"containers": []}}
    assert apply_startup_policy(pod, StageStartupPolicy(), request_uid=10001) is pod


def test_complete_committed_execution_map_keeps_normal_qualification_with_registry(tmp_path, bundle):
    profiles = ScientificProfileCatalog.load(CATALOG_ROOT)
    document = json.loads((CATALOG_ROOT / "contracts/scientific-execution-map.json").read_text())
    document["snapshot_bundles"] = {bundle["bundle_id"]: bundle}
    path = tmp_path / "execution-map.json"
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
    renderer = FileScientificManifestRenderer(path=path, profiles=profiles)
    assert len(document["models"]) == 10
    for profile in profiles.list():
        assert renderer.execution_map_sha256 == "sha256:" + profile.value["qualification"]["execution_map_sha256"]
    assert renderer.execution_configuration_sha256 != renderer.execution_map_sha256
    assert all(execution.startup_policy.backend == "normal-load" for execution in renderer.executions.values())
    selection = {"sample-structure": {"backend": "cuda-criu", "bundle_id": bundle["bundle_id"]}}
    assert renderer.validate_startup_policy_overrides(model_id="protenix-v2", overrides=selection) == selection
    assert renderer.startup_policy_options("protenix-v2")["sample-structure"] == [bundle["bundle_id"]]
    renderer.executions = MappingProxyType({
        key: replace(execution, image="runtime@sha256:" + "a" * 64)
        if key == ("protenix-v2", "sample-structure") else execution
        for key, execution in renderer.executions.items()
    })
    assert renderer.startup_policy_options("protenix-v2")["sample-structure"] == []
    with pytest.raises(ScientificExecutionMapError, match="identity"):
        renderer.validate_startup_policy_overrides(model_id="protenix-v2", overrides=selection)


def setup_renderer(tmp_path, bundle):
    # Reuse the real execution-map fixture, adapting only its synthetic stage
    # name/image/revision to the qualified production Protenix stage identity.
    base = runtime_execution_map(tmp_path)
    path = tmp_path / "complete.json"
    document = json.loads(path.read_text())
    model = document["models"][0]
    model["stages"][1]["stage_id"] = "sample-structure"
    for stage in model["stages"]:
        stage["image"] = bundle["runtime_image"]
    document["snapshot_bundles"] = {bundle["bundle_id"]: bundle}
    path.write_text(json.dumps(document))
    value = copy.deepcopy(dict(runtime_profile().value))
    value["workload"]["stages"][1]["id"] = "sample-structure"
    value["execution_identity"]["runtime_image_digest"] = bundle["runtime_image"].split("@", 1)[1]
    value["execution_identity"]["model_revision"] = bundle["profile_model_revision"]
    profile = ScientificWorkloadProfile(MappingProxyType(value))
    catalog = ScientificProfileCatalog(
        profiles={profile.model_id: profile}, validators=ScientificProfileCatalog.load(CATALOG_ROOT)._validators
    )
    renderer = FileScientificManifestRenderer(
        path=path,
        profiles=catalog,
        tools_image=base.tools_image,
        internal_api_url=base.internal_api_url,
        capability_authority=base.capability_authority,
    )
    plan = runtime_plan()
    plan = replace(
        plan,
        controller_plan=replace(
            plan.controller_plan,
            stages=(
                plan.controller_plan.stages[0],
                replace(plan.controller_plan.stages[1], stage_id="sample-structure"),
            ),
        ),
        invocations=(plan.invocations[0], replace(plan.invocations[1], stage_id="sample-structure")),
    )
    access = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")
    localized = renderer.verify_runtime_artifacts(profile, plan, access)
    plan = renderer.bind_runtime_artifacts(profile, plan, access, localized)
    return renderer, profile, plan, access, localized


def test_admin_options_validation_freeze_codec_and_real_pod_renderer(tmp_path, bundle, monkeypatch):
    renderer, profile, normal_plan, access, localized = setup_renderer(tmp_path, bundle)
    selection = {"sample-structure": {"backend": "cuda-criu", "bundle_id": bundle["bundle_id"]}}
    assert renderer.validate_startup_policy_overrides(model_id="protenix-v2", overrides=selection) == selection
    assert renderer.startup_policy_options("protenix-v2") == {"prepare": [], "sample-structure": [bundle["bundle_id"]]}
    with pytest.raises(ScientificExecutionMapError, match="no model stage"):
        renderer.validate_startup_policy_overrides(
            model_id="protenix-v2", overrides={"absent": selection["sample-structure"]}
        )
    with pytest.raises(ScientificExecutionMapError, match="differs"):
        renderer.validate_startup_policy_overrides(
            model_id="protenix-v2", overrides={"prepare": selection["sample-structure"]}
        )
    plan = renderer.bind_startup_policies(profile, normal_plan, selection)
    assert plan.execution_binding("prepare") == normal_plan.execution_binding("prepare")
    snapshot = scheduling(plan.controller_plan)
    input_id = uuid4()
    state = ScientificBatchState.admit(
        operation_id=uuid4(),
        tenant_id="tenant-a",
        plan=plan.controller_plan,
        scheduling=snapshot,
        model_id="protenix-v2",
        variant_id=plan.variant_id,
        input_artifact_id=input_id,
        execution_plan=plan,
        access_context=access,
        runtime_artifacts=localized,
        input_manifest=VerifiedInputManifest(
            manifest_id="inputs",
            manifest_artifact_id=input_id,
            manifest_digest="sha256:" + "1" * 64,
            entries=(
                ScientificInputArtifact(
                    logical_artifact_id="raw-request",
                    semantic_type="request/v1",
                    artifact_id=uuid4(),
                    digest="sha256:" + "2" * 64,
                    size_bytes=2,
                    media_type="application/json",
                ),
            ),
        ),
    )
    reopened = state_from_value(state_to_value(state))
    assert reopened == state
    resource = WorkloadResource(
        operation_id=state.operation_id,
        batch_id=state.batch_id,
        workload_id=state.workload_id,
        attempt_id=uuid4(),
        stage_id="sample-structure",
        shard_id="main",
        attempt_number=1,
        tenant_id="tenant-a",
        model_id="protenix-v2",
        variant_id=plan.variant_id,
        input_artifact_id=input_id,
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="protenix-test",
        kind=WorkloadKind.JOB,
        scheduling=snapshot.stage("sample-structure"),
        invocation=plan.invocation("sample-structure", "main"),
        access_context=access,
        runtime_artifacts=localized,
        execution_map_sha256=plan.execution_map_sha256,
        execution_binding=normal_plan.execution_binding("sample-structure"),
        materializations=(
            ResolvedArtifactMaterialization.resolve(
                plan.invocation("sample-structure", "main").materializations[0],
                artifact_id=uuid4(),
                digest="sha256:" + "3" * 64,
                size_bytes=1,
                media_type="application/x-tar",
                compression=None,
            ),
        ),
    )
    normal = renderer.render(resource)
    restored = renderer.render(replace(resource, execution_binding=plan.execution_binding("sample-structure")))
    normal_spec, restored_spec = (item["spec"]["template"]["spec"] for item in (normal, restored))
    assert restored_spec["containers"][1] == normal_spec["containers"][1]
    assert restored_spec["initContainers"][1:] == normal_spec["initContainers"]
    assert restored_spec["containers"][0]["resources"] == normal_spec["containers"][0]["resources"]
    assert restored_spec["affinity"] == normal_spec["affinity"]
    assert "hostPID" not in restored_spec and "nodeName" not in restored_spec
    assert (
        restored_spec["containers"][0]["command"][-len(normal_spec["containers"][0]["command"]) :]
        == normal_spec["containers"][0]["command"]
    )
    monkeypatch.syspath_prepend(str(SOLUTION_ROOT / "acceptance/h100-fleet/snapshots"))
    measured = importlib.import_module("render_scientific_restore").scientific_restore(
        normal, {**bundle, "name": "comparison"}
    )
    # Probe-only code resets termination grace and inserts an empty selector;
    # production deliberately retains the original scheduler/termination fields.
    expected = measured["spec"]
    expected["terminationGracePeriodSeconds"] = normal_spec["terminationGracePeriodSeconds"]
    if "nodeSelector" not in normal_spec:
        expected.pop("nodeSelector", None)
    assert restored_spec == expected
    renderer.snapshot_bundles = MappingProxyType({})
    assert (
        renderer.render(
            replace(resource, execution_binding=reopened.execution_plan.execution_binding("sample-structure"))
        )
        == restored
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("policy_failure", ["removed-bundle", "resolver-error"])
async def test_submit_resolves_admin_choice_once_and_reuses_captured_selection(cipher, hasher, pending, policy_failure):
    from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository
    from test_scientific_batch_production import (
        FakeArtifactAccess,
        FakeExecutionBinding,
        FakePlanFactory,
        principal,
        profile_catalog,
    )
    from test_scientific_batch_production import (
        scheduling as service_scheduling,
    )

    from fs2_serve.memory_store import MemoryStore
    from fs2_serve.scientific_batch.catalog_adapter import CatalogProfileAdapterError
    from fs2_serve.scientific_batch.controller import ScientificBatchController
    from fs2_serve.scientific_batch.service import ScientificBatchService

    original = {"design": {"backend": "normal-load", "bundle_id": None}}
    resolved_calls, bound_choices = [], []

    async def resolver(*, model_id, tenant_id):
        resolved_calls.append((model_id, tenant_id))
        return original

    class Binding(FakeExecutionBinding):
        def bind_startup_policies(self, profile, plan, overrides):
            if overrides["design"]["backend"] != "normal-load":
                raise CatalogProfileAdapterError("snapshot bundle was removed")
            bound_choices.append(copy.deepcopy(overrides))
            original["design"] = {"backend": "cuda-criu", "bundle_id": "changed-after-capture"}
            return plan

    store = MemoryStore(cipher, hasher)
    identity = await principal(store)
    repository = FakeScientificBatchRepository()
    controller = ScientificBatchController(
        repository=repository,
        cluster=FakeScientificBatchCluster(),
        controller_id="controller-a",
        namespace="fs2-models",
    )
    pointer = {"artifact_id": str(uuid4()), "sha256": "1" * 64, "size_bytes": 100, "media_type": "application/json"}
    service = ScientificBatchService(
        store=store,
        repository=repository,
        controller=controller,
        profiles=profile_catalog(),
        scheduling=service_scheduling(),
        artifacts=FakeArtifactAccess(pointer),
        execution_binding=Binding(),
        plan_factory=FakePlanFactory(),
        startup_policy_resolver=resolver,
    )
    request = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "design",
        "service_class": "customer-batch",
        "input_manifest": pointer,
        "parameters": {},
    }
    materialize = service._materialize_admission

    async def stopped_before_materialization(operation_id):
        raise RuntimeError("simulated process stop after durable acceptance")

    if pending:
        service._materialize_admission = stopped_before_materialization
    arguments = dict(
        principal=identity,
        model_id="protein-design",
        idempotency_key="startup-freeze-once-01",
        request=request,
    )
    if pending:
        with pytest.raises(RuntimeError, match="simulated process stop"):
            await service.submit(**arguments)
        assert len(await store.list_scientific_admissions()) == 1
    else:
        await service.submit(**arguments)
    assert resolved_calls == [("protein-design", "tenant-a")]
    assert bound_choices == [{"design": {"backend": "normal-load", "bundle_id": None}}] * 2

    async def unavailable_resolver(**kwargs):
        raise ValueError("snapshot policy resolver unavailable")

    if policy_failure == "resolver-error":
        service.startup_policy_resolver = unavailable_resolver
    service._materialize_admission = materialize
    replay = await service.submit(**arguments)
    assert replay["operation"]["reused"] is True
    assert len(store.operations) == 1
    assert not await store.list_scientific_admissions()
    assert bound_choices == [{"design": {"backend": "normal-load", "bundle_id": None}}] * 2
    with pytest.raises(ValueError if policy_failure == "resolver-error" else ScientificProfileError):
        await service.submit(**{**arguments, "idempotency_key": "startup-new-request-02"})
    assert len(store.operations) == 1
