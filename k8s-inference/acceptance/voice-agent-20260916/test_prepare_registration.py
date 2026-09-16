import copy

import pytest
from test_model_deployment import envelope, model_spec

from fs2_serve.model_deployment import (
    InfrastructureEnvelope,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    RenderContext,
)
from fs2_serve.model_deployment_controller import ControllerFiles
from prepare_registration import IDS, append_models


def inputs():
    original = envelope().model_dump(mode="json", by_alias=True)
    pool = original["pools"]["pool-a"]
    original["pools"] = {
        "l40s-1x": {
            **pool,
            "poolId": "l40s-1x",
            "acceleratorClass": "nvidia-l40s-48gb",
            "nodeSelector": {"accelerator.fs2.nebius/pool-id": "l40s-1x"},
        }
    }
    spec = model_spec().model_dump(mode="json", by_alias=True)
    spec["cache"]["tier"] = "Disabled"
    spec["exposure"].update(openAI=False, openAIAliases=[])
    return (
        original,
        [],
        {"schema": "fs2-serve.nebius.ai/deployment-runtime-set/v1", "models": {}},
        spec,
    )


def test_voice_registration_preserves_pools_siblings_and_scales_matching_services():
    source = inputs()
    before = copy.deepcopy(source)
    actual, bundles, selections, proposals = append_models(*source)
    assert source == before
    assert actual["pools"] == before[0]["pools"]
    assert (
        actual["qualifications"]["qwen.3-8b"]
        == before[0]["qualifications"]["qwen.3-8b"]
    )
    contract = ControllerFiles(
        infrastructure_envelope=InfrastructureEnvelope.model_validate(actual),
        bundles=[LegacyTemplateBundle.model_validate(b) for b in bundles],
    )
    for proposal in proposals:
        identity = proposal["name"]
        qualification = actual["qualifications"][identity]
        assert (
            not qualification["scaleToZeroQualified"]
            and not qualification["gpuSnapshotBundles"]
        )
        assert not selections["models"][identity]["qualification"]["states"][
            "http_mcp_qualified"
        ]
        for replicas in (1, 2):
            scaled = copy.deepcopy(proposal["spec"])
            scaled["availability"].update(minReplicas=replicas, maxReplicas=2)
            spec = ModelDeploymentSpec.model_validate(scaled)
            assert spec.policy.allowed_principal_ids == []
            rendered = contract.renderer().render(
                spec,
                RenderContext(
                    name=identity,
                    namespace=proposal["namespace"],
                    generation=replicas,
                    pool=contract.infrastructure_envelope.pools["l40s-1x"],
                    eligible_pools=list(
                        contract.infrastructure_envelope.pools.values()
                    ),
                    prometheus_server_address="http://prometheus.example:9090",
                    preview=True,
                ),
            )
            workloads = [
                r.manifest for r in rendered.resources if r.kind == "Deployment"
            ]
            services = [r.manifest for r in rendered.resources if r.kind == "Service"]
            assert workloads and services
            for workload in workloads:
                labels = workload["spec"]["template"]["metadata"]["labels"]
                assert any(
                    all(labels.get(k) == v for k, v in svc["spec"]["selector"].items())
                    for svc in services
                )
                assert (
                    workload["spec"]["template"]["spec"]["containers"][0]["name"]
                    == "voice"
                )
                assert (
                    workload["spec"]["template"]["spec"][
                        "terminationGracePeriodSeconds"
                    ]
                    == 1900
                )


def test_voice_registration_never_overwrites_existing_models():
    source = inputs()
    source[0]["qualifications"][IDS[0]] = {}
    with pytest.raises(ValueError, match="already registered"):
        append_models(*source)
