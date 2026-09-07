import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def test_exact_qualified_template_and_native_oracle_are_preserved():
    entry = json.loads((ROOT / "catalog/runtime/deployment-runtimes/openfold3-portable-h100.json").read_text())
    documents = list(yaml.safe_load_all((ROOT / "models/structure/openfold3-preview2/k8s.yaml").read_text()))
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    runtime = pod["containers"][0]
    fragment = json.loads((HERE / "integration.json").read_text())
    assert runtime["image"] == entry["record"]["runtime"]["image"]["reference"] == fragment["image"]
    assert runtime["resources"] == fragment["resources"]
    assert "command" not in runtime and "args" not in runtime
    assert {e["name"]: e["value"] for e in runtime["env"]} == {
        "MAX_JOBS": "8",
        "FS2_RUNTIME_CACHE_ROOT": fragment["cache"]["runtime_path"].replace(
            fragment["compile_cache_abi"], "deployment-profile-abi-v1"
        ),
    }
    assert "nodeSelector" not in pod
    service = next(item for item in documents if item["kind"] == "Service")
    assert service["metadata"]["name"] == entry["qualification"]["active_runtime"]["service"]["name"]
    assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
    profiles = json.loads((ROOT / "catalog/profiles/model-profiles.json").read_text())
    assert profiles["model_artifacts"]["openfold3"]["manifest_paths"] == [fragment["source_manifest"]]
    assert profiles["model_autoscaling_targets"]["openfold3"]["deployment"] == deployment["metadata"]["name"]
    compatibility = json.loads((ROOT / "catalog/profiles/model-accelerator-compatibility.json").read_text())
    selected = compatibility["models"]["openfold3"]["runtimes"][entry["variant_id"]]
    assert selected["runtime_ref"] == runtime["image"]
    assert selected["bindings"][0]["accelerator_class"] == "nvidia-h100-sxm5-80gb"
    assert "catalog-canonical" in compatibility["models"]["openfold3"]["runtimes"]
    validator = entry["record"]["semantic_validator"]
    for path_key, digest_key in (("source_path", "source_sha256"), ("fixture_path", "fixture_sha256")):
        assert hashlib.sha256((ROOT / "catalog/runtime/packaged-repository" / validator[path_key]).read_bytes()).hexdigest() == validator[digest_key]


def test_three_measured_cached_processes_and_first_compile_are_separate():
    raw = (HERE / "qualification.json").read_bytes()
    report = json.loads(raw)
    entry = json.loads((ROOT / "catalog/runtime/deployment-runtimes/openfold3-portable-h100.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == entry["qualification"]["evidence"]["cold_start_acceptance_sha256"]
    trials = report["cached_image_fresh_process_trials"]
    assert len({row["pod_uid"] for row in trials}) == 3
    assert all(len(row["semantic"]["cases"]) == 3 for row in trials)
    assert report["initial_compile_cohort"]["seconds"]["first_request_to_validated_response"] > 300
    assert report["statistics"]["container_to_model_ready"]["n"] == 3
    assert not entry["qualification"]["states"]["http_mcp_qualified"]
