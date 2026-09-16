import copy

import pytest
from test_model_deployment import envelope, model_spec

from fs2_serve.model_deployment import InfrastructureEnvelope, LegacyTemplateBundle, ModelDeploymentSpec, RenderContext
from fs2_serve.model_deployment_controller import ControllerFiles
from prepare_registration import IDS, append_models


def inputs():
    original = envelope().model_dump(mode="json", by_alias=True)
    pool = original["pools"]["pool-a"]
    original["pools"] = {
        name: {**pool, "poolId": name, "acceleratorClass": "nvidia-h100-sxm5-80gb",
               "nodeSelector": {"accelerator.fs2.nebius/pool-id": name}}
        for name in ("h100-1x", "h100-reserved-8x")
    }
    spec = model_spec().model_dump(mode="json", by_alias=True)
    spec["cache"]["tier"] = "Disabled"
    spec["exposure"].update(openAI=False, openAIAliases=[])
    return original, [], {"schema": "fs2-serve.nebius.ai/deployment-runtime-set/v1", "models": {}}, spec


def test_additive_registration_preserves_input_and_has_no_false_qualification():
    source = inputs()
    before = copy.deepcopy(source)
    actual, bundles, selections, proposals = append_models(*source)
    assert source == before
    assert actual["pools"] == before[0]["pools"]
    assert actual["qualifications"]["qwen.3-8b"] == before[0]["qualifications"]["qwen.3-8b"]
    for identity in IDS:
        qualification = actual["qualifications"][identity]
        assert not qualification["scaleToZeroQualified"] and not qualification["gpuSnapshotBundles"]
        assert not selections["models"][identity]["qualification"]["states"]["http_mcp_qualified"]
    contract = ControllerFiles(infrastructure_envelope=InfrastructureEnvelope.model_validate(actual),
                               bundles=[LegacyTemplateBundle.model_validate(b) for b in bundles])
    for proposal in proposals:
        spec = ModelDeploymentSpec.model_validate(proposal["spec"])
        assert spec.policy.allowed_principal_ids == []
        rendered = contract.renderer().render(spec, RenderContext(
            name=proposal["name"], namespace=proposal["namespace"], generation=1,
            pool=contract.infrastructure_envelope.pools["h100-1x"],
            eligible_pools=list(contract.infrastructure_envelope.pools.values()),
            prometheus_server_address="http://prometheus.example:9090", preview=True,
        ))
        assert rendered.resources
        for resource in rendered.resources:
            if resource.kind == "ScaledObject":
                assert len("keda-hpa-" + resource.name) <= 63
            if resource.kind == "Deployment":
                container = resource.manifest["spec"]["template"]["spec"]["containers"][0]
                assert {item["name"]: item.get("value") for item in container["env"]}["TMPDIR"] == "/cache"
                assert container["resources"]["limits"]["ephemeral-storage"] == "24Gi"


def test_refuses_to_overwrite_an_existing_registration():
    source = inputs()
    source[0]["qualifications"][IDS[0]] = {}
    with pytest.raises(ValueError, match="already registered"):
        append_models(*source)


def test_template_update_keeps_every_current_template_and_sibling_qualification():
    from prepare_template_update import extend
    original, bundles, _, _ = append_models(*inputs())
    before = copy.deepcopy((original, bundles))
    updated, actual_bundles, references = extend(original, bundles)
    assert (original, bundles) == before
    assert all(bundle in actual_bundles for bundle in bundles)
    assert updated["pools"] == original["pools"]
    assert updated["qualifications"]["qwen.3-8b"] == original["qualifications"]["qwen.3-8b"]
    for identity in IDS:
        qualification = updated["qualifications"][identity]
        assert set(original["qualifications"][identity]["templateDigests"]) <= set(qualification["templateDigests"])
        assert references[identity]["name"] in qualification["templateRefs"]
