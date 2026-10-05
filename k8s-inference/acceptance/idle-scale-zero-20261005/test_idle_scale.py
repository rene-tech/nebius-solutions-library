import copy
import json

import pytest
import yaml

from test_model_deployment import envelope, model_spec
from fs2_serve.model_deployment import InfrastructureEnvelope, ModelDeploymentSpec, RenderContext
from fs2_serve.model_deployment_controller import ControllerFiles
from prepare_managed import MODELS, ROOT, TOOLS, append_models
from remove_hot_floors import proposal_for


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
