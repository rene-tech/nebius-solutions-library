import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from fs2_serve.serving_snapshot import ServingSnapshotBundle

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("small_media_snapshot_probe", HERE / "small_media_snapshot_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize("model", ["sdxl", "nv-segment-ct"])
def test_small_media_snapshot_keeps_qualified_native_command_identity_and_settings(model):
    source = next(item for item in yaml.safe_load_all((ROOT / "models/general-media/k8s" / (model + ".yaml")).read_text())
                  if item["kind"] == "Deployment")
    image = json.loads((HERE / "integration.json").read_bytes())["models"][model]["image"]
    original = source["spec"]["template"]["spec"]["containers"][0]
    original["image"] = image
    before = copy.deepcopy(source)
    args = SimpleNamespace(model=model, run="unit-run", pod="unit-pod", node="unit-h100",
                           source_configmap="fs2-fleet-snapshot-serving-workdir-v8")
    pod = probe.donor(source, args)
    runtime = pod["spec"]["containers"][0]
    offset = runtime["command"].index("--") + 1
    assert runtime["command"][offset:offset + 9] == ["python3", "/snapshot-source/working_directory_launcher.py",
        "--directory", "/vllm-workspace", "--uid", "1000", "--gid", "1000", "--"]
    assert runtime["command"][offset + 9:] == original["command"]
    assert runtime["image"] == image
    assert runtime["resources"] == original["resources"]
    assert all(value in runtime["env"] for value in original["env"])
    assert "fsGroup" not in pod["spec"]["securityContext"]
    assert "fsGroupChangePolicy" not in pod["spec"]["securityContext"]
    assert source == before


@pytest.mark.parametrize("prefix", ["cxr", "segment"])
def test_bundle_binds_six_measured_trials_and_exact_capture_identity(prefix):
    report_path = HERE / (prefix + "-snapshot-qualification.json")
    report = json.loads(report_path.read_bytes())
    bundle = ServingSnapshotBundle.model_validate_json((HERE / (prefix + "-snapshot-bundle.json")).read_text())
    assert bundle.qualification_receipt_sha256 == hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert bundle.manifest_sha256 == report["bundle"]["manifest_sha256"]
    assert bundle.runtime_image == report["compatibility"]["runtime_identity"]["runtime_image"]
    assert bundle.source_sha256 == report["compatibility"]["runtime_identity"]["snapshot_source_sha256"]
    assert len(report["runs"]) == 6
    assert all(row["gpu_pod_deleted"] and len(row["requests"]) == 2 for row in report["runs"])
    assert not report["production_selectable"]
    assert "not unseen-input evidence" in report["semantic_scope"]
