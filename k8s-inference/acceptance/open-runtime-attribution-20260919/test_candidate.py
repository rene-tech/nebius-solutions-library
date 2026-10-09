import copy
from pathlib import Path

import prepare_candidate as p
import pytest
import yaml

IMAGE = "registry.example.invalid/diffdock@sha256:" + "b" * 64


def original():
    resources = list(yaml.safe_load_all((p.ROOT / "models/structure/manifests/diffdock.yaml").read_text()))
    bundle = {
        "modelRef": "diffdock",
        "runtimeProfile": "custom",
        "templateDigest": p.terraform_digest(resources),
        "primaryWorkloadName": "diffdock-b300",
        "runtimeContainerName": "runtime",
        "primaryServiceName": "diffdock-b300",
        "primaryServicePort": 8000,
        "resources": resources,
    }
    owner = {
        "modelRef": "diffdock",
        "artifact": {"revision": "exact-test-revision"},
        "runtime": {
            "image": "registry.test/diffdock@" + p.BASE_DIGEST,
            "templateRef": {"name": "legacy", "digest": bundle["templateDigest"]},
        },
        "cache": {"tier": "SharedFilesystem", "snapshotPreference": "Prefer", "snapshotRef": {"name": "old"}},
        "fastStart": {"level": "Off"},
        "availability": {"minReplicas": 1, "maxReplicas": 4},
        "placement": {"poolRefs": ["available-h100", "other-qualified-pool"]},
    }
    return bundle, owner


def test_immutable_template_only_changes_wrapper_identity_and_image():
    before, owner = original()
    saved = copy.deepcopy(before)
    after = p.candidate_template(before, IMAGE, owner["artifact"]["revision"])
    assert before == saved
    expected = copy.deepcopy(before)
    deployment = next(r for r in expected["resources"] if r["kind"] == "Deployment")
    pod = deployment["spec"]["template"]
    for meta in [deployment["metadata"], pod["metadata"]]:
        meta["annotations"]["fs2.nebius/runtime-image-digest"] = IMAGE.split("@")[1]
        meta["annotations"]["fs2.nebius/model-revision"] = owner["artifact"]["revision"]
    pod["metadata"]["annotations"][p.ANNOTATION] = p.VERSION
    runtime = pod["spec"]["containers"][0]
    runtime["image"] = IMAGE
    runtime["command"] = p.COMMAND
    runtime["env"].append(p.UID_ENV)
    expected["templateDigest"] = p.terraform_digest(expected["resources"])
    assert after == expected
    proposed = p.proposal(owner, before, after, IMAGE)
    for key in set(owner) - {"runtime", "cache"}:
        assert proposed[key] == owner[key]
    assert proposed["cache"] == {"tier": "SharedFilesystem", "snapshotPreference": "Never"}
    assert owner["cache"]["snapshotRef"] == {"name": "old"}


@pytest.mark.parametrize("fault", ["base", "command", "args", "port", "uid", "annotation", "duplicate_deployment"])
def test_unreviewed_template_rejected(fault):
    bundle, _ = original()
    deployment = next(r for r in bundle["resources"] if r["kind"] == "Deployment")
    pod = deployment["spec"]["template"]
    runtime = pod["spec"]["containers"][0]
    if fault == "base":
        runtime["image"] = IMAGE
    elif fault == "command":
        runtime["command"] = ["python3", "/different.py"]
    elif fault == "args":
        runtime["args"] = ["different"]
    elif fault == "port":
        runtime["env"][0]["value"] = "9000"
    elif fault == "uid":
        runtime["env"].append(p.UID_ENV)
    elif fault == "annotation":
        pod["metadata"]["annotations"][p.ANNOTATION] = "old"
    else:
        bundle["resources"].append(copy.deepcopy(deployment))
    with pytest.raises(ValueError):
        p.candidate_template(bundle, IMAGE, "exact-test-revision")


@pytest.mark.parametrize("fault", ["image", "template", "fast_start"])
def test_stale_owner_or_unqualified_fast_start_rejected(fault):
    bundle, owner = original()
    candidate = p.candidate_template(bundle, IMAGE, owner["artifact"]["revision"])
    if fault == "image":
        owner["runtime"]["image"] = IMAGE
    elif fault == "template":
        owner["runtime"]["templateRef"]["digest"] = "changed"
    else:
        owner["fastStart"]["level"] = "L2"
    with pytest.raises(ValueError):
        p.proposal(owner, bundle, candidate, IMAGE)


def test_derivative_only_copies_wrapper_from_exact_qualified_base():
    text = (p.ROOT / "models/structure/runtime/common/Dockerfile.response-identity").read_text()
    instructions = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert instructions[0].endswith("@" + p.BASE_DIGEST)
    assert sum(line.startswith("COPY") for line in instructions) == 1
    assert not any(line.startswith(("RUN", "ENV", "CMD", "ENTRYPOINT")) for line in instructions)
    assert "/opt/fs2/runtime/common/server.py" in text
    assert Path(p.ROOT / "models/structure/runtime/common/server.py").is_file()
