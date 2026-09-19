"""Real retained template -> real renderer -> strict response verifier regression."""

import copy
import json
from pathlib import Path

import prepare_candidate as candidate
import prepare_revision as repair
import pytest

from fs2_serve.model_deployment import (
    LegacyManifestRenderer,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    PoolEnvelope,
    RenderContext,
)
from fs2_serve.runtime_kubernetes import KubernetesRuntimeMetadataProvider


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/retained-template-r178.json").read_bytes())


def rendered(corrected):
    saved = fixture()
    bundle, owner = saved["template"], saved["owner_spec"]
    if corrected:
        bundle = candidate.complete_revision_template(bundle, owner)
        owner["runtime"]["templateRef"] = {"name": candidate.REVISION_TEMPLATE_NAME, "digest": bundle["templateDigest"]}
    spec = ModelDeploymentSpec.model_validate(owner)
    pools = [PoolEnvelope.model_validate(saved["pools"][ref]) for ref in spec.placement.pool_refs]
    renderer = LegacyManifestRenderer(
        {("diffdock", bundle["templateDigest"]): LegacyTemplateBundle.model_validate(bundle)}
    )
    context = RenderContext(
        name="diffdock",
        namespace="fs2-models",
        generation=16,
        pool=pools[0],
        eligible_pools=pools,
        prometheus_server_address="http://prometheus.fs2-observability.svc:9090",
        preview=True,
    )
    plan = renderer.render(spec, context)
    service = next(r.manifest for r in plan.resources if r.kind == "Service")
    service["metadata"]["uid"] = "test-service-uid"
    deployments = [r.manifest for r in plan.resources if r.kind == "Deployment"]
    return spec, service, deployments


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [False, True])
async def test_retained_template_render_is_accepted_only_with_exact_revision(corrected):
    spec, service, deployments = rendered(corrected)
    assert len(deployments) == 2  # Both ordinary hot and burst templates.
    for deployment in deployments:
        pod = copy.deepcopy(deployment["spec"]["template"])
        pod["metadata"].update(name="test-model-pod", namespace="fs2-models", uid="test-pod-uid")
        pod["status"] = {"podIP": "10.0.0.2", "containerStatuses": [{"name": "runtime", "imageID": spec.runtime.image}]}
        slice_value = {
            "metadata": {
                "labels": {"kubernetes.io/service-name": service["metadata"]["name"]},
                "ownerReferences": [{"kind": "Service", "uid": "test-service-uid"}],
            },
            "ports": [{"port": 8000, "protocol": "TCP"}],
            "endpoints": [
                {
                    "addresses": ["10.0.0.2"],
                    "conditions": {"ready": True},
                    "targetRef": {
                        "kind": "Pod",
                        "uid": "test-pod-uid",
                        "name": "test-model-pod",
                        "namespace": "fs2-models",
                    },
                }
            ],
        }

        class Reader:
            def __init__(self, endpoint_slice):
                self.endpoint_slice = endpoint_slice

            async def list(self, path):
                if path.endswith("/services"):
                    return [service]
                if path.endswith("/endpointslices"):
                    return [self.endpoint_slice]
                raise AssertionError("unexpected runtime read: " + path)

        provider = KubernetesRuntimeMetadataProvider(Reader(slice_value))
        binding = {
            "service_name": service["metadata"]["name"],
            "service_port": 8000,
            "runtime_image_digest": spec.runtime.image.rsplit("@", 1)[1],
            "model_revision": spec.artifact.revision,
        }
        assert await provider._response_matches_endpoint(pod, binding) is corrected
        if corrected:
            binding["model_revision"] = "different-model-revision"
            assert not await provider._response_matches_endpoint(pod, binding)


def test_repair_changes_only_revision_annotations_and_template_identity():
    saved = fixture()
    previous, owner = saved["template"], saved["owner_spec"]
    before = copy.deepcopy(previous)
    after = candidate.complete_revision_template(previous, owner)
    assert previous == before
    expected = copy.deepcopy(previous)
    deployment = next(r for r in expected["resources"] if r["kind"] == "Deployment")
    for metadata in (deployment["metadata"], deployment["spec"]["template"]["metadata"]):
        metadata.setdefault("annotations", {})["fs2.nebius/model-revision"] = owner["artifact"]["revision"]
    expected["templateDigest"] = candidate.terraform_digest(expected["resources"])
    assert after == expected


@pytest.mark.parametrize("fault", ["revision_conflict", "owner_digest", "image", "port", "uid", "entrypoint"])
def test_unreviewed_metadata_repair_is_rejected(fault):
    saved = fixture()
    template, owner = saved["template"], saved["owner_spec"]
    deployment = next(r for r in template["resources"] if r["kind"] == "Deployment")
    pod = deployment["spec"]["template"]
    runtime = next(c for c in pod["spec"]["containers"] if c["name"] == "runtime")
    if fault == "revision_conflict":
        pod["metadata"]["annotations"]["fs2.nebius/model-revision"] = "different"
    elif fault == "owner_digest":
        owner["runtime"]["templateRef"]["digest"] = "sha256:" + "a" * 64
    elif fault == "image":
        runtime["image"] = runtime["image"].replace("sha256:", "sha256:changed")
    elif fault == "port":
        next(e for e in runtime["env"] if e["name"] == "FS2_PORT")["value"] = "9000"
    elif fault == "uid":
        runtime["env"] = [e for e in runtime["env"] if e["name"] != "FS2_RUNTIME_POD_UID"]
    else:
        runtime["command"] = ["python3", "/unqualified.py"]
    with pytest.raises(ValueError):
        candidate.complete_revision_template(template, owner)


def test_revision_extension_preserves_minimum_replicas_snapshots_and_other_qualifications():
    saved = fixture()
    template, spec = saved["template"], saved["owner_spec"]
    digest = template["templateDigest"]
    envelope = {
        "revision": "old",
        "snapshot_marker": [{"name": "retained"}],
        "qualifications": {
            "sibling": {"original": True},
            "diffdock": {
                "templateRefs": {"original": digest},
                "templateDigests": [digest],
                "templateCacheTiers": {digest: ["NodeLocal"]},
            },
        },
    }
    owner = {"metadata": {"name": "diffdock", "namespace": "fs2-models"}, "spec": spec}
    changed, bundles, [proposal] = repair.extend(envelope, [template], owner)
    assert bundles[0] == template
    assert changed["snapshot_marker"] == envelope["snapshot_marker"]
    assert changed["qualifications"]["sibling"] == envelope["qualifications"]["sibling"]
    for key in set(spec) - {"runtime"}:
        assert proposal["spec"][key] == spec[key]
    assert proposal["spec"]["availability"]["minReplicas"] == 2
    assert proposal["spec"]["runtime"]["image"] == spec["runtime"]["image"]
