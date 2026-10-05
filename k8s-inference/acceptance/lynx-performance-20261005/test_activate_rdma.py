import copy
import importlib.util
import sys

import activate_rdma as release
import pytest
from test_prepare_rdma import fixtures as fabric_fixture

path = release.A.HERE / "test_activate_mpi.py"
sys.path.insert(0, str(path.parent))
spec = importlib.util.spec_from_file_location("_mpi_activation_fixture", path)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def fixtures():
    (api, execution, scheduling_cm, live, source, profiles), options = base.fixture()
    scheduling, queue, admin, envelope, nodes = fabric_fixture()
    scheduling["model_eligible_pool_ids"] = {"gromacs-mpi": [release.fabric.POOL]}
    scheduling_cm = release.A.configmap(
        scheduling_cm,
        "kueue-scheduling.json",
        release.A.canonical(scheduling),
        "digest",
    )
    image = options["expected_api"]
    template = api["spec"]["template"]
    template["spec"]["initContainers"] = [{"name": "wait-schema", "image": image}]
    env = template["spec"]["containers"][0]["env"]
    release.A.named(env, "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE", "env")["value"] = image
    release.A.named(env, "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256", "env")[
        "value"
    ] = release.A.sha(release.A.canonical(scheduling))
    release.A.named(
        template["spec"]["volumes"], "scientific-batch-scheduling", "volume"
    )["configMap"]["name"] = scheduling_cm["metadata"]["name"]
    cms = {"execution": execution, "scheduling": scheduling_cm}
    for kind, value in (("admin", admin), ("envelope", envelope)):
        key = release.fabric.VOLUMES[kind][1]
        cms[kind] = release.A.configmap(
            {"metadata": {"name": "fs2-" + kind + "-old"}},
            key,
            release.A.canonical(value),
            "digest",
        )
        template["spec"]["volumes"].append(
            {
                "name": release.fabric.VOLUMES[kind][0],
                "configMap": {"name": cms[kind]["metadata"]["name"]},
            }
        )
    api["metadata"] = {"uid": "api-uid", "generation": 3}
    api["status"] = {"observedGeneration": 3}
    controller = {
        "metadata": {"uid": "controller-uid", "generation": 4},
        "status": {"observedGeneration": 4},
        "spec": {
            "replicas": 2,
            "template": {
                "metadata": {"annotations": {"keep": "yes"}},
                "spec": {
                    "containers": [
                        {
                            "name": "model-controller",
                            "image": image,
                            "env": [{"name": "keep", "value": "yes"}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "infrastructure-envelope",
                            "configMap": {"name": cms["envelope"]["metadata"]["name"]},
                        },
                        {"name": "keep", "secret": {"secretName": "unchanged"}},
                    ],
                },
            },
        },
    }
    deployments = {"api": api, "controller": controller}
    rs, pods = [], []
    for role, deployment in deployments.items():
        rsid = role + "-rs"
        rs.append(
            {
                "metadata": {
                    "uid": rsid,
                    "ownerReferences": [
                        {"uid": deployment["metadata"]["uid"], "controller": True}
                    ],
                }
            }
        )
        for index in range(deployment["spec"]["replicas"]):
            pods.append(
                {
                    "metadata": {
                        "name": f"{role}-{index}",
                        "uid": f"{role}-{index}",
                        "ownerReferences": [{"uid": rsid, "controller": True}],
                    },
                    "spec": copy.deepcopy(deployment["spec"]["template"]["spec"]),
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            )
    return (
        (deployments, cms, live, source, profiles, queue, nodes),
        {
            "image": options["image"],
            "compatible_image": image,
            "catalog_digest": "sha256:" + "a" * 64,
            "expected_queue_drift": None,
        },
        rs,
        pods,
    )


def test_composed_patch_preserves_unrelated_configuration_and_never_mutates_inputs():
    args, kwargs, _, _ = fixtures()
    before = copy.deepcopy(args)
    result = release.prepare(*args, **kwargs)
    assert args == before
    api = result["api_patch"][-1]["value"]
    controller = result["controller_patch"][-1]["value"]
    assert result["api_patch"][0] == {
        "op": "test",
        "path": "/metadata/uid",
        "value": "api-uid",
    }
    assert api["spec"]["initContainers"][0]["image"] == kwargs["image"]
    assert api["spec"]["containers"][0]["image"] == kwargs["image"]
    assert api["metadata"]["annotations"]["unrelated"] == "keep"
    assert (
        api["metadata"]["annotations"]["fs2.nebius.ai/catalog-rollout-digest"]
        == kwargs["catalog_digest"]
    )
    assert (
        controller["spec"]["volumes"][1]
        == before[0]["controller"]["spec"]["template"]["spec"]["volumes"][1]
    )
    assert controller["spec"]["containers"][0]["env"] == [
        {"name": "keep", "value": "yes"}
    ]
    assert controller["metadata"]["annotations"]["keep"] == "yes"
    assert set(result["configmaps"]) == {"execution", "scheduling", "admin", "envelope"}
    for cm in result["configmaps"].values():
        assert cm["immutable"] is True
        assert cm["metadata"]["name"].endswith(
            "-" + release.A.sha(next(iter(cm["data"].values())).encode())[:12]
        )


def test_ready_reader_barrier_records_exact_fleet():
    args, kwargs, rs, pods = fixtures()
    assert len(release.barrier(args[0], rs, pods, kwargs["compatible_image"])) == 5


@pytest.mark.parametrize(
    "change",
    [
        "old",
        "terminating",
        "extra",
        "missing",
        "unready",
        "generation",
        "init",
        "tools",
    ],
)
def test_reader_barrier_rejects_any_unproven_reader(change):
    args, kwargs, rs, pods = fixtures()
    if change == "old":
        pods[0]["spec"]["containers"][0]["image"] = "old"
    elif change == "terminating":
        pods[0]["metadata"]["deletionTimestamp"] = "now"
    elif change == "extra":
        pods.append(copy.deepcopy(pods[0]))
    elif change == "missing":
        pods.pop()
    elif change == "unready":
        pods[-1]["status"]["conditions"][0]["status"] = "False"
    elif change == "generation":
        args[0]["api"]["status"]["observedGeneration"] = 2
    elif change == "init":
        pods[0]["spec"]["initContainers"][0]["image"] = "old"
    else:
        release.A.named(
            pods[0]["spec"]["containers"][0]["env"],
            "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE",
            "env",
        )["value"] = "old"
    with pytest.raises(ValueError):
        release.barrier(args[0], rs, pods, kwargs["compatible_image"])


@pytest.mark.parametrize(
    "change",
    ["controller-image", "controller-envelope", "api-init", "mutable-cm", "digest"],
)
def test_proposal_rejects_baseline_drift(change):
    args, kwargs, _, _ = fixtures()
    if change == "controller-image":
        args[0]["controller"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "old"
        )
    elif change == "controller-envelope":
        args[0]["controller"]["spec"]["template"]["spec"]["volumes"][0]["configMap"][
            "name"
        ] = "other"
    elif change == "api-init":
        args[0]["api"]["spec"]["template"]["spec"]["initContainers"][0]["image"] = "old"
    elif change == "mutable-cm":
        args[1]["admin"]["immutable"] = False
    else:
        kwargs["catalog_digest"] = "invented"
    with pytest.raises(ValueError):
        release.prepare(*args, **kwargs)
