import copy

import pytest
import yaml
from parser_candidate import PARSERS, ROOT, isolated_pod, parser_template, proposal


def baseline():
    docs = list(yaml.safe_load_all((ROOT / "models/general-media/k8s/qwen3-8b.yaml").read_text()))
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["args"][-len(PARSERS) :] == PARSERS
    container["args"] = container["args"][: -len(PARSERS)]
    return {
        "modelRef": "qwen3-8b",
        "runtimeContainerName": "vllm",
        "resources": [deployment],
        "templateDigest": "sha256:" + "a" * 64,
    }


def test_only_parser_argv_and_derived_digest_change():
    before = baseline()
    original = copy.deepcopy(before)
    candidate = parser_template(before)
    assert before == original
    container = candidate["resources"][0]["spec"]["template"]["spec"]["containers"][0]
    assert container["args"][-len(PARSERS) :] == PARSERS
    container["args"] = container["args"][: -len(PARSERS)]
    candidate["templateDigest"] = before["templateDigest"]
    assert candidate == before


def test_off_proposal_cannot_restore_old_parserless_snapshot():
    original = {
        "modelRef": "qwen3-8b",
        "fastStart": {"level": "Off"},
        "runtime": {"templateRef": {"name": "old", "digest": "old"}, "image": "same"},
        "cache": {"tier": "SharedFilesystem", "snapshotPreference": "Prefer", "snapshotRef": {"name": "old"}},
        "availability": {"minReplicas": 0, "maxReplicas": 8},
    }
    candidate = parser_template(baseline())
    after = proposal(original, candidate)
    assert after["cache"] == {"tier": "SharedFilesystem", "snapshotPreference": "Never"}
    assert "snapshotRef" in original["cache"]
    assert after["availability"] == original["availability"]
    assert after["runtime"]["image"] == original["runtime"]["image"]
    pod = isolated_pod(candidate, node="isolated-existing", name="qualification", registry="registry.test")
    assert all(not v["name"].startswith("snapshot-") for v in pod["spec"]["volumes"])
    assert "initContainers" not in pod["spec"]
    assert all(not c.get("command") for c in pod["spec"]["containers"])
    assert next(v for v in pod["spec"]["volumes"] if v["name"] == "model")["persistentVolumeClaim"]["readOnly"]


@pytest.mark.parametrize("mutation", ["image", "wrapped", "already_parser", "context"])
def test_reject_wrong_baseline(mutation):
    before = baseline()
    container = before["resources"][0]["spec"]["template"]["spec"]["containers"][0]
    if mutation == "image":
        container["image"] = "different"
    elif mutation == "wrapped":
        container["command"] = ["snapshot-entrypoint", "restore"]
    elif mutation == "already_parser":
        container["args"].extend(PARSERS)
    else:
        container["args"][container["args"].index("--max-model-len") + 1] = "65536"
    with pytest.raises(ValueError):
        parser_template(before)
