"""Retained GPU evidence binds native speech candidates, not public readiness."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog/runtime"


def read(path):
    return json.loads(path.read_text())


@pytest.mark.parametrize("short,model", [
    ("en", "nemotron-speech-en-0-6b"),
    ("multi", "nemotron-speech-multilingual-0-6b"),
])
def test_exact_worker_receipts_and_no_fabricated_public_qualification(short, model):
    native = read(CATALOG / f"native/{model}.json")
    selected = read(CATALOG / f"deployment-runtimes/{model}.json")
    path = ROOT / f"acceptance/nemotron-speech-20260916/{short}-native-contract-r3.json"
    receipt = read(path)
    assert native["record"] == selected["record"]
    assert native["record"]["runtime"]["image"]["reference"] == receipt["image"]
    assert receipt["status"] == "passed"
    files = [row for row in receipt["measurements"] if row["mode"] == "http-file"]
    live = [row for row in receipt["measurements"] if row["mode"] == "websocket-unpaced"]
    assert len(files) == len(live) == 2
    assert len({row["result"]["text"] for row in files}) == 2
    for file, stream, tail in zip(files, live, ("telescope", "microscope"), strict=True):
        assert tail in file["result"]["text"].lower()
        assert file["result"]["text"] == stream["result"]["text"]
        assert file["result"]["audio_seconds"] == stream["result"]["audio_seconds"]
        assert file["input_sha256"] == stream["input_sha256"]
    assert native["semantic_requests"]["requests"] == [
        {"id": row["case"], "payload_sha256": row["request_payload_sha256"]} for row in files
    ]
    assert selected["qualification"]["evidence"]["retained_deployments_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert selected["qualification"]["states"] == {
        "registered": True, "runtime_ready": True, "semantic_qualified": True,
        "route_active": False, "http_mcp_qualified": False,
        "cold_start_qualified": False, "elasticity_qualified": False,
    }
    assert native["record"]["startup"]["enabled_mechanisms"] == ["conventional"]
    for prefix in ("source", "fixture"):
        semantic = native["record"]["semantic_validator"]
        relative = semantic[f"{prefix}_path"]
        actual = (ROOT.parent / relative).read_bytes()
        assert (CATALOG / "packaged-repository" / relative).read_bytes() == actual
        assert hashlib.sha256(actual).hexdigest() == semantic[f"{prefix}_sha256"]


@pytest.mark.parametrize("model", ["nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b"])
def test_worker_profile_and_regional_artifact_configuration(model):
    record = read(CATALOG / f"native/{model}.json")["record"]
    deployment, service = list(yaml.safe_load_all((ROOT / f"models/speech/k8s/{model}.yaml").read_text()))
    pod = deployment["spec"]["template"]["spec"]
    container, = pod["containers"]
    assert container["image"] == record["runtime"]["image"]["reference"]
    assert container["command"] == record["runtime"]["command"]
    assert deployment["spec"]["replicas"] == 0
    assert deployment["spec"]["selector"]["matchLabels"] == service["spec"]["selector"]
    assert int(container["resources"]["requests"]["nvidia.com/gpu"]) == record["resources"]["gpu"]["count"] == 1
    assert container["resources"]["requests"]["memory"] == "16Gi"
    assert pod["terminationGracePeriodSeconds"] > 7200
    env = {row["name"]: row for row in container["env"]}
    profile = json.loads(env["FS2_SPEECH_PROFILE_JSON"]["value"])
    assert profile["model"] == model.replace("0-6b", "0.6b")
    assert profile["chunk_size_ms"] == 560 and profile["cuda_graphs"] is False
    assert env["FS2_SPEECH_ARTIFACT_HOSTS"]["valueFrom"]["configMapKeyRef"] == {
        "name": "fs2-speech-runtime-settings", "key": "artifact-hosts",
    }
    contract = read(ROOT / "components/control-plane/src/fs2_serve/model_input_schemas/speech.json")[model]
    assert contract["properties"]["options"]["properties"]["model"]["const"] == profile["model"]
    profiles = read(ROOT / "catalog/profiles/model-profiles.json")
    Draft202012Validator(read(ROOT / "catalog/profiles/model-profiles.schema.json")).validate(profiles)
    assert model in profiles["managed_native_model_ids"]
    assert model in profiles["profiles"]["speech"]["canonical_routes"]
