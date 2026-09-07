from __future__ import annotations

import copy

import pytest
from test_model_deployment import digest, envelope, model_spec, render_context, renderer

from fs2_serve.model_deployment import (
    CacheSpec,
    CacheTier,
    LegacyManifestRenderer,
    SnapshotPreference,
    SnapshotRef,
    SnapshotStrategy,
    ValidationDisposition,
    validate_model_deployment,
)
from fs2_serve.model_deployment_controller import ControllerFiles
from fs2_serve.serving_snapshot import ServingSnapshotBundle


def test_snapshot_registry_accepts_future_model_ids_without_code_changes():
    source, infrastructure, template, config, context = fixture()
    future_id = "future-model-1"
    config = ServingSnapshotBundle.model_validate({**config.model_dump(by_alias=True), "model_ref": future_id})
    qualification = infrastructure.qualifications.pop(source.model_ref)
    qualification.model_ref = source.model_ref = template.model_ref = future_id
    qualification.gpu_snapshot_bundles = {config.bundle_id: config}
    infrastructure.qualifications[future_id] = qualification
    decision = validate_model_deployment(source, infrastructure)
    assert decision.disposition is ValidationDisposition.ACCEPTED
    configured = ControllerFiles(infrastructure_envelope=infrastructure, bundles=[template]).renderer()
    plan = configured.render(source, context)
    assert any(item.kind == "Deployment" for item in plan.resources)
    with pytest.raises(ValueError):
        ServingSnapshotBundle.model_validate({**config.model_dump(by_alias=True), "model_ref": "../unknown model"})


def test_configuration_options_expose_only_renderable_snapshot_bundles(cipher, hasher):
    from test_model_deployment_mutation import PROMETHEUS_ADDRESS, FakeWriter

    from fs2_serve.memory_store import MemoryStore
    from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
    from fs2_serve.model_deployment_mutation import ModelDeploymentMutationService

    source, infrastructure, template, config, _context = fixture()
    infrastructure.qualifications[source.model_ref].max_accelerators_per_replica = 1
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(MemoryStore(cipher, hasher)),
        writer=FakeWriter(),
        envelope=infrastructure,
        renderer=ControllerFiles(infrastructure_envelope=infrastructure, bundles=[template]).renderer(),
        prometheus_server_address=PROMETHEUS_ADDRESS,
    )
    option = service.configuration_options()[0]
    assert option.default_spec.cache.snapshot_preference is SnapshotPreference.NEVER
    assert option.default_spec.cache.snapshot_ref is None
    assert [choice.bundle_id for choice in option.gpu_snapshot_choices] == [config.bundle_id]
    choice = option.gpu_snapshot_choices[0]
    assert choice.pool_refs == ["pool-a"]
    assert choice.digest == "sha256:" + config.manifest_sha256
    assert choice.compatibility == config.compatibility
    infrastructure.qualifications[source.model_ref].gpu_snapshot_bundles[config.bundle_id] = config.model_copy(
        update={"runtime_image": "registry.example/changed@" + digest("9")}
    )
    assert service.configuration_options()[0].gpu_snapshot_choices == []


def fixture():
    source = model_spec().model_copy(update={"model_ref": "qwen3-8b"}, deep=True)
    source.placement.pool_refs = ["pool-a"]
    config = ServingSnapshotBundle.model_validate(
        {
            "schema": "fs2-serve.nebius.ai/serving-snapshot-bundle/v1",
            "bundle_id": "qwen-measured-v1",
            "model_ref": "qwen3-8b",
            "runtime_image": source.runtime.image,
            "model_revision": source.artifact.revision,
            "artifact_manifest_digest": source.artifact.manifest_digest,
            "manifest_sha256": "1" * 64,
            "qualification_receipt_sha256": "2" * 64,
            "qualified": True,
            "accelerator_classes": ["nvidia-h100-sxm"],
            "compatibility": {
                "gpu_name": "NVIDIA H100 80GB HBM3",
                "compute_capability": "9.0",
                "driver_version": "580.159.04",
                "kernel_release": "6.11.0-1016-nvidia",
            },
            "tools_image": "registry.example/tools@" + digest("3"),
            "source_configmap": "captured-source",
            "source_sha256": {
                name: "4" * 64
                for name in (
                    "process_checkpoint.py",
                    "serving_checkpoint.py",
                    "serving_filesystem.py",
                    "serving_launcher.py",
                    "serving_supervisor.py",
                    "sitecustomize.py",
                    "supervisor.py",
                )
            },
            "entrypoint_configmap": "entrypoint-v1",
            "entrypoint_sha256": "5" * 64,
            "network_configmap": "network-v1",
            "address_configmap": "address-v1",
            "pvc": "snapshot-rwx",
            "bundle_path": "qwen-v1",
            "captured_pod_ip": "10.0.0.5",
            "image_entrypoint": ["vllm", "serve"],
            "runtime_command": ["vllm", "serve", "model", "--dtype", "bfloat16"],
        }
    )
    source.cache = CacheSpec(
        tier=CacheTier.SHARED_FILESYSTEM,
        snapshot_preference=SnapshotPreference.PREFER,
        snapshot_ref=SnapshotRef(
            name=config.bundle_id, digest="sha256:" + config.manifest_sha256, strategy=SnapshotStrategy.CUDA_CHECKPOINT
        ),
    )
    infrastructure = envelope().model_copy(deep=True)
    qualification = infrastructure.qualifications.pop("qwen.3-8b")
    qualification.model_ref = source.model_ref
    qualification.template_cache_tiers = {digest("c"): CacheTier.SHARED_FILESYSTEM}
    qualification.gpu_snapshot_bundles = {config.bundle_id: config}
    infrastructure.qualifications[source.model_ref] = qualification
    bundle = next(iter(renderer()._bundles.values())).model_copy(update={"model_ref": source.model_ref}, deep=True)
    runtime = bundle.resources[0]["spec"]["template"]["spec"]["containers"][0]
    runtime["args"] = ["model", "--dtype", "bfloat16"]
    for name in ("startupProbe", "readinessProbe"):
        runtime[name] = {
            "httpGet": {"path": "/health", "port": 8000},
            "periodSeconds": 5,
            "timeoutSeconds": 1,
            "failureThreshold": 30,
        }
    context = render_context().model_copy(
        update={
            "pool": infrastructure.pools["pool-a"],
            "eligible_pools": [infrastructure.pools["pool-a"]],
        }
    )
    return source, infrastructure, bundle, config, context


def test_registered_snapshot_is_admitted_and_preserves_scheduling_resources_and_requests():
    source, infrastructure, bundle, config, context = fixture()
    decision = validate_model_deployment(source, infrastructure)
    assert decision.disposition is ValidationDisposition.ACCEPTED, decision.issues
    context.fast_start_mechanism = decision.fast_start_mechanism
    files = ControllerFiles(infrastructure_envelope=infrastructure, bundles=[bundle])
    bundle.resources[0]["spec"]["template"]["spec"]["securityContext"] = {
        "fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch", "supplementalGroups": [42],
    }
    plan = files.renderer().render(source, context)
    deployment = next(resource.manifest for resource in plan.resources if resource.kind == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    runtime = pod["containers"][0]
    assert pod["nodeSelector"] == infrastructure.pools["pool-a"].node_selector
    assert runtime["resources"] == bundle.resources[0]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert runtime["command"][-len(config.runtime_command) :] == config.runtime_command
    assert runtime["command"][runtime["command"].index("--fallback") + 1] == "normal-load"
    assert runtime["readinessProbe"]["exec"]["command"][-1] == "ready"
    assert runtime["readinessProbe"]["failureThreshold"] == 30
    assert next(volume for volume in pod["volumes"] if volume["name"] == "snapshot-bundle")["persistentVolumeClaim"][
        "readOnly"
    ]
    assert not pod.get("hostNetwork")
    assert pod["securityContext"] == {"supplementalGroups": [42, 1000]}
    assert all("nvidia.com/gpu" not in init.get("resources", {}).get("limits", {}) for init in pod["initContainers"])


@pytest.mark.parametrize("change", ["gpu", "image", "artifact", "strategy"])
def test_other_runtime_or_gpu_cannot_advertise_the_snapshot(change):
    source, infrastructure, _bundle, _config, _context = fixture()
    if change == "gpu":
        source.placement.pool_refs = ["pool-b"]
    elif change == "image":
        source.runtime.image = "registry.example/other@" + digest("9")
    elif change == "artifact":
        source.artifact.revision = "different-revision"
    else:
        source.cache.snapshot_ref.strategy = SnapshotStrategy.WEIGHTS
    decision = validate_model_deployment(source, infrastructure)
    assert decision.disposition is not ValidationDisposition.ACCEPTED
    assert "snapshot_runtime_unqualified" in {issue.code for issue in decision.issues}


def test_require_disables_fallback_and_changed_argv_cannot_reuse_cached_state():
    source, infrastructure, bundle, config, context = fixture()
    source.cache.snapshot_preference = SnapshotPreference.REQUIRE
    configured = ControllerFiles(infrastructure_envelope=infrastructure, bundles=[bundle]).renderer()
    plan = configured.render(source, context)
    workload = next(item.manifest for item in plan.resources if item.kind == "Deployment")
    command = workload["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command[command.index("--fallback") + 1] == "fail"
    changed = copy.deepcopy(bundle)
    changed.resources[0]["spec"]["template"]["spec"]["containers"][0]["args"].append("--changed")
    renderer_ = LegacyManifestRenderer(
        {(source.model_ref, bundle.template_digest): changed},
        snapshot_bundles={(source.model_ref, config.bundle_id): config},
    )
    with pytest.raises(ValueError, match="original arguments"):
        renderer_.render(source, context)


def test_normal_loading_ignores_available_optional_bundle():
    source, infrastructure, bundle, _config, context = fixture()
    source.cache = CacheSpec(tier=CacheTier.SHARED_FILESYSTEM, snapshot_preference=SnapshotPreference.NEVER)
    with_bundle = (
        ControllerFiles(infrastructure_envelope=infrastructure, bundles=[bundle]).renderer().render(source, context)
    )
    without_bundle = LegacyManifestRenderer({(source.model_ref, bundle.template_digest): bundle}).render(
        source, context
    )
    assert with_bundle == without_bundle
