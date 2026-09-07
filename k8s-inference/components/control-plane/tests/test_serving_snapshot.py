from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

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
from fs2_serve.serving_snapshot import DEFAULT_SUPERVISOR_PATH, ServingSnapshotBundle, configure_serving_snapshot

SOLUTION_ROOT = Path(__file__).resolve().parents[3]


def direct_pod(config):
    probe = {"httpGet": {"path": "/health", "port": 8000}, "periodSeconds": 5}
    return {
        "containers": [{"name": "model", "image": config.runtime_image, "command": config.runtime_command,
                        "env": [{"name": "UNCHANGED", "value": "test"}],
                        "resources": {"requests": {"nvidia.com/gpu": "1"}, "limits": {"nvidia.com/gpu": "1"}},
                        "volumeMounts": [], "readinessProbe": dict(probe), "startupProbe": dict(probe)}],
        "volumes": [], "securityContext": {"fsGroup": 1000},
    }


@pytest.mark.parametrize("model,expected", [
    ("qwen3-8b", "aaf75536c3257fb41a30a5cfb97d2f11a918c0edfc945ef4ca45f12a3eef9525"),
    ("cosmos3-nano", "f97eed1b998575b8700f90e1f8e1a483a3fd898c02bb7370423cc80124c73522"),
    ("genmol", "4dcd72d9d5bdefc91bb7b27cb718cef8185600467fe20dc61451bcc34ea1119f"),
])
def test_published_bundles_keep_byte_identical_render_with_default_interpreters(model, expected):
    # These hashes were measured before adding interpreter/PATH configuration.
    config = ServingSnapshotBundle.model_validate_json(
        (SOLUTION_ROOT / f"acceptance/h100-fleet/snapshots/{model}-bundle.json").read_text()
    )
    assert config.supervisor_python == config.address_python == "python3"
    assert config.supervisor_path == DEFAULT_SUPERVISOR_PATH
    pod = direct_pod(config)
    configure_serving_snapshot(pod, config=config, runtime_container_name="model", fallback="normal-load")
    actual = hashlib.sha256(json.dumps(pod, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert actual == expected


def test_captured_interpreters_change_only_snapshot_wrapper_and_keep_native_argv():
    _source, _infrastructure, _template, config, _context = fixture()
    payload = config.model_dump(by_alias=True)
    interpreter = "/opt/openfold3/.pixi/envs/openfold3-cuda12/bin/python3"
    payload.update(supervisor_python=interpreter, address_python=interpreter,
                   supervisor_path=str(Path(interpreter).parent) + ":" + DEFAULT_SUPERVISOR_PATH)
    selected = ServingSnapshotBundle.model_validate(payload)
    before, after = direct_pod(config), direct_pod(selected)
    configure_serving_snapshot(before, config=config, runtime_container_name="model", fallback="normal-load")
    configure_serving_snapshot(after, config=selected, runtime_container_name="model", fallback="normal-load")
    expected = copy.deepcopy(before)
    runtime = expected["containers"][0]
    runtime["command"][0] = interpreter
    next(item for item in runtime["env"] if item["name"] == "PATH")["value"] = selected.supervisor_path
    for name in ("startupProbe", "readinessProbe"):
        runtime[name]["exec"]["command"][0] = interpreter
    expected["initContainers"][0]["command"][0] = interpreter
    assert after == expected
    assert after["containers"][0]["command"][-len(config.runtime_command):] == config.runtime_command


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


def test_configuration_options_expose_snapshot_for_readiness_only_template(
    cipher, hasher
):
    """A long readiness gate is also a valid supervisor startup gate."""
    from test_model_deployment_mutation import PROMETHEUS_ADDRESS, FakeWriter

    from fs2_serve.memory_store import MemoryStore
    from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
    from fs2_serve.model_deployment_mutation import ModelDeploymentMutationService

    source, infrastructure, template, config, _context = fixture()
    infrastructure.qualifications[source.model_ref].max_accelerators_per_replica = 1
    runtime = template.resources[0]["spec"]["template"]["spec"]["containers"][0]
    original_readiness = copy.deepcopy(runtime["readinessProbe"])
    runtime.pop("startupProbe")
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(MemoryStore(cipher, hasher)),
        writer=FakeWriter(),
        envelope=infrastructure,
        renderer=ControllerFiles(
            infrastructure_envelope=infrastructure, bundles=[template]
        ).renderer(),
        prometheus_server_address=PROMETHEUS_ADDRESS,
    )

    option = service.configuration_options()[0]

    assert [choice.bundle_id for choice in option.gpu_snapshot_choices] == [
        config.bundle_id
    ]
    snapshot_choice = option.gpu_snapshot_choices[0]
    candidate = option.default_spec.model_copy(deep=True)
    candidate.placement.pool_refs = snapshot_choice.pool_refs
    candidate.cache = CacheSpec(
        tier=CacheTier.SHARED_FILESYSTEM,
        snapshot_preference=SnapshotPreference.PREFER,
        snapshot_ref=SnapshotRef(
            name=config.bundle_id,
            digest="sha256:" + config.manifest_sha256,
            strategy=SnapshotStrategy.CUDA_CHECKPOINT,
        ),
    )
    decision = validate_model_deployment(candidate, infrastructure)
    context = render_context().model_copy(
        update={
            "pool": infrastructure.pools[decision.admitted_pool_ref],
            "eligible_pools": [
                infrastructure.pools[pool_ref]
                for pool_ref in candidate.placement.pool_refs
            ],
            "fast_start_mechanism": decision.fast_start_mechanism,
        }
    )
    plan = service.renderer.render(candidate, context)
    deployment = next(
        resource.manifest for resource in plan.resources if resource.kind == "Deployment"
    )
    rendered = deployment["spec"]["template"]["spec"]["containers"][0]
    assert rendered["startupProbe"] == rendered["readinessProbe"]
    assert rendered["startupProbe"]["exec"]["command"][-1] == "ready"
    assert original_readiness["failureThreshold"] == 30


def test_snapshot_storage_is_independent_of_the_template_artifact_cache_tier(
    cipher, hasher
):
    """A bundle PVC can accelerate a runtime whose weights are image-baked."""
    from test_model_deployment_mutation import PROMETHEUS_ADDRESS, FakeWriter

    from fs2_serve.memory_store import MemoryStore
    from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
    from fs2_serve.model_deployment_mutation import ModelDeploymentMutationService

    source, infrastructure, template, config, _context = fixture()
    qualification = infrastructure.qualifications[source.model_ref]
    qualification.max_accelerators_per_replica = 1
    qualification.template_cache_tiers[template.template_digest] = CacheTier.NODE_LOCAL
    service = ModelDeploymentMutationService(
        repository=StoreModelDeploymentRepository(MemoryStore(cipher, hasher)),
        writer=FakeWriter(),
        envelope=infrastructure,
        renderer=ControllerFiles(
            infrastructure_envelope=infrastructure, bundles=[template]
        ).renderer(),
        prometheus_server_address=PROMETHEUS_ADDRESS,
    )

    option = service.configuration_options()[0]

    assert option.default_spec.cache.tier is CacheTier.NODE_LOCAL
    assert [choice.bundle_id for choice in option.gpu_snapshot_choices] == [
        config.bundle_id
    ]


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


def test_working_directory_fallback_is_exactly_bound_to_its_captured_source():
    source, infrastructure, bundle, config, context = fixture()
    payload = config.model_dump(by_alias=True)
    payload["source_sha256"] = {
        **payload["source_sha256"],
        "working_directory_launcher.py": "6" * 64,
    }
    payload["fallback_command_prefix"] = [
        "python3",
        "/snapshot-source/working_directory_launcher.py",
        "--directory",
        "/opt/fs2",
        "--uid",
        "1000",
        "--gid",
        "1000",
        "--",
    ]
    config = ServingSnapshotBundle.model_validate(payload)
    infrastructure.qualifications[source.model_ref].gpu_snapshot_bundles = {
        config.bundle_id: config
    }
    plan = ControllerFiles(
        infrastructure_envelope=infrastructure, bundles=[bundle]
    ).renderer().render(source, context)
    workload = next(item.manifest for item in plan.resources if item.kind == "Deployment")
    command = workload["spec"]["template"]["spec"]["containers"][0]["command"]
    separator = command.index("--")
    assert command[separator + 1 : separator + 10] == config.fallback_command_prefix
    assert command[separator + 10 :] == config.runtime_command

    missing_source = {**payload, "source_sha256": fixture()[3].source_sha256}
    with pytest.raises(ValueError, match="working-directory source"):
        ServingSnapshotBundle.model_validate(missing_source)
    unused_source = {**payload, "fallback_command_prefix": ["python3", "/snapshot-source/serving_launcher.py"]}
    with pytest.raises(ValueError, match="working-directory source"):
        ServingSnapshotBundle.model_validate(unused_source)


def test_published_genmol_bundle_uses_the_qualified_working_directory_launcher():
    payload = json.loads(
        (
            SOLUTION_ROOT
            / "acceptance/h100-fleet/snapshots/genmol-bundle.json"
        ).read_bytes()
    )
    config = ServingSnapshotBundle.model_validate(payload)
    assert config.model_ref == "genmol"
    assert config.fallback_command_prefix == [
        "python3",
        "/snapshot-source/working_directory_launcher.py",
        "--directory",
        "/opt/fs2",
        "--uid",
        "1000",
        "--gid",
        "1000",
        "--",
    ]
