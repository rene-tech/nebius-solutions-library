import copy
import json

import pytest
import yaml

from test_model_deployment import envelope, model_spec
from fs2_serve.model_deployment import InfrastructureEnvelope, ModelDeploymentSpec, RenderContext
from fs2_serve.model_deployment_controller import ControllerFiles
from prepare_managed import MODELS, ROOT, TOOLS, append_models, source_resources
from remove_hot_floors import proposal_for
from retained_adoptions import SOURCE, configmaps, merge_registration, retained_runtimes


def inputs():
    original = envelope().model_dump(mode="json", by_alias=True)
    pool = original["pools"]["pool-a"]
    original["pools"] = {"h100-ondemand-1x": {
        **pool, "poolId": "h100-ondemand-1x", "acceleratorClass": "nvidia-h100-sxm5-80gb",
        "nodeSelector": {"accelerator.fs2.nebius/pool-id": "h100-ondemand-1x"},
    }}
    spec = model_spec().model_dump(mode="json", by_alias=True)
    spec["cache"]["tier"] = "Disabled"
    spec["exposure"].update(openAI=False, openAIAliases=[])
    selections = {"models": {model: json.loads((ROOT / "catalog/runtime/deployment-runtimes" /
                                               (model + ".json")).read_text()) for model in MODELS}}
    resources = {model: list(yaml.safe_load_all((ROOT / "models/visual-science/k8s" /
                                               (model + ".yaml")).read_text())) for model in MODELS}
    return original, [], selections, spec, resources


def test_floor_change_preserves_every_other_desired_setting():
    current = {"name": "example", "namespace": "fs2-models", "etag": "revision",
               "spec": model_spec().model_dump(mode="json", by_alias=True)}
    current["spec"]["availability"]["minReplicas"] = 1
    before = copy.deepcopy(current)
    proposal = proposal_for(current)
    assert current == before
    proposal["spec"]["availability"]["minReplicas"] = 1
    assert proposal["spec"] == before["spec"]
    assert proposal["base_etag"] == "revision"


def test_registration_preserves_pools_and_siblings_and_renders_one_scale_owner():
    source = inputs()
    before = copy.deepcopy(source)
    actual, bundles, proposals = append_models(*source)
    assert source == before
    assert actual["pools"] == before[0]["pools"]
    assert actual["qualifications"]["qwen.3-8b"] == before[0]["qualifications"]["qwen.3-8b"]
    contract = ControllerFiles(infrastructure_envelope=InfrastructureEnvelope.model_validate(actual), bundles=bundles)
    for proposal in proposals:
        spec = ModelDeploymentSpec.model_validate(proposal["spec"])
        assert spec.exposure.mcp_tool_name == TOOLS[proposal["name"]]
        assert spec.availability.min_replicas == 0 and spec.availability.max_replicas == 1
        assert not actual["qualifications"][proposal["name"]]["scaleToZeroQualified"]
        pool = contract.infrastructure_envelope.pools["h100-ondemand-1x"]
        render = contract.renderer().render(spec, RenderContext(
            name=proposal["name"], namespace=proposal["namespace"], uid="test-owner", generation=1,
            pool=pool, eligible_pools=[pool], prometheus_server_address="http://prometheus.example:9090"))
        workloads = [r.manifest for r in render.resources if r.kind == "Deployment"]
        scalers = [r.manifest for r in render.resources if r.kind == "ScaledObject"]
        assert len(workloads) == len(scalers) == 1
        assert "replicas" not in workloads[0]["spec"]
        assert scalers[0]["spec"]["minReplicaCount"] == 0
        assert scalers[0]["spec"]["scaleTargetRef"]["name"] == workloads[0]["metadata"]["name"]
        assert 'state=~"queued|activating|running"' in scalers[0]["spec"]["triggers"][0]["metadata"]["query"]


def test_registration_refuses_existing_model_overwrite():
    source = inputs()
    source[0]["qualifications"][MODELS[0]] = {}
    with pytest.raises(ValueError, match="already registered"):
        append_models(*source)


def all_pool_inputs():
    source = inputs()
    for pool_id, accelerator in (("l40s-1x", "nvidia-l40s-48gb"),
                                 ("wan2-h200-1x", "nvidia-h200-sxm5-141gb")):
        source[0]["pools"][pool_id] = {
            **source[0]["pools"]["h100-ondemand-1x"],
            "poolId": pool_id, "acceleratorClass": accelerator,
            "nodeSelector": {"accelerator.fs2.nebius/pool-id": pool_id},
        }
    return source


def test_all_eight_sources_render_zero_floor_under_the_existing_owner():
    source = all_pool_inputs()
    models = tuple(TOOLS)
    source[4].update({model: source_resources(model) for model in models})
    actual, bundles, proposals = append_models(*source, models=models)
    assert len(proposals) == 8
    contract = ControllerFiles(infrastructure_envelope=InfrastructureEnvelope.model_validate(actual), bundles=bundles)
    for proposal in proposals:
        spec = ModelDeploymentSpec.model_validate(proposal["spec"])
        pool = contract.infrastructure_envelope.pools[spec.placement.pool_refs[0]]
        render = contract.renderer().render(spec, RenderContext(
            name=proposal["name"], namespace=proposal["namespace"], uid="test-owner", generation=1,
            pool=pool, eligible_pools=[pool], prometheus_server_address="http://prometheus.example:9090"))
        workloads = [r.manifest for r in render.resources if r.kind == "Deployment"]
        scalers = [r.manifest for r in render.resources if r.kind == "ScaledObject"]
        assert len(workloads) == len(scalers) == 1
        assert "replicas" not in workloads[0]["spec"]
        assert scalers[0]["spec"]["minReplicaCount"] == 0
        assert scalers[0]["spec"]["maxReplicaCount"] == 1
        assert len(workloads[0]["metadata"]["ownerReferences"]) == 1


def test_retained_bundle_sources_are_exact_and_idempotent():
    envelope, bundles, *_ = all_pool_inputs()
    source = json.loads(SOURCE.read_text())
    before = copy.deepcopy((envelope, bundles, source))
    merged = merge_registration(envelope, bundles, source)
    assert (envelope, bundles, source) == before
    assert merge_registration(*merged, source) == merged
    assert merged[0]["pools"] == envelope["pools"]
    assert merged[0]["qualifications"]["qwen.3-8b"] == envelope["qualifications"]["qwen.3-8b"]
    assert all(item["immutable"] for item in configmaps(*merged)["items"])
    # Historical retained ACE bundle has replicas=1. Its canonical source is
    # now cold, and the renderer never writes replicas for any managed App.
    for model in source["modelIds"]:
        resources = source_resources(model)
        assert next(r for r in resources if r["kind"] == "Deployment")["spec"]["replicas"] == 0


def test_retained_registration_refuses_missing_pool_or_changed_model():
    envelope, bundles, *_ = all_pool_inputs()
    source = json.loads(SOURCE.read_text())
    del envelope["pools"]["wan2-h200-1x"]
    with pytest.raises(ValueError, match="missing declared compatible pool"):
        merge_registration(envelope, bundles, source)
    envelope, bundles, *_ = all_pool_inputs()
    envelope["qualifications"]["mindguard-4b"] = {"different": True}
    with pytest.raises(ValueError, match="different existing model qualification"):
        merge_registration(envelope, bundles, source)


def test_retained_runtime_records_are_exact_and_native_rows_are_not_fabricated():
    source = json.loads(SOURCE.read_text())
    runtimes = retained_runtimes(source)
    assert len(runtimes) == 6
    assert {"mindguard-4b", "mindguard-8b"}.isdisjoint(runtimes)
    source["runtimeSources"]["scvi-scanvi"]["sha256"] = "a" * 64
    with pytest.raises(ValueError, match="changed runtime source digest"):
        retained_runtimes(source)


def test_retained_registration_requires_exact_pool_not_just_same_gpu_class():
    envelope, bundles, *_ = all_pool_inputs()
    source = json.loads(SOURCE.read_text())
    source["requiredPoolRefs"]["wan2-2-i2v-nim"] = ["undeclared-h200"]
    with pytest.raises(ValueError, match="missing declared compatible pool"):
        merge_registration(envelope, bundles, source)


def test_retained_registration_rejects_changed_bundle_digest():
    envelope, bundles, *_ = all_pool_inputs()
    source = json.loads(SOURCE.read_text())
    source["bundles"][0]["resources"][0]["metadata"]["annotations"] = {"changed": "true"}
    with pytest.raises(AssertionError):
        merge_registration(envelope, bundles, source)
