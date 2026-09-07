import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import yaml
from fs2_serve.serving_snapshot import ServingSnapshotBundle

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("of3_snapshot_probe_tested", HERE / "snapshot_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_snapshot_preserves_native_bash_identity_resources_and_model_cache():
    source = next(item for item in yaml.safe_load_all((ROOT / "models/structure/openfold3-preview2/k8s.yaml").read_text()) if item["kind"] == "Deployment")
    before = copy.deepcopy(source)
    image = source["spec"]["template"]["spec"]["containers"][0]["image"]
    dockerfile = (ROOT / "models/structure/openfold3-preview2/Dockerfile").read_text()
    entrypoint = json.loads(next(line.removeprefix("ENTRYPOINT ") for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT ")))
    config = {"image": image, "entrypoint": entrypoint, "user": "10001:10001", "working_directory": "/opt/fs2/openfold3-preview2"}
    args = SimpleNamespace(run="of3-unit", pod="of3-unit", node="test-h100", source_configmap="fs2-fleet-snapshot-serving-workdir-v8")
    pod = probe.donor(source, config, args)
    runtime = next(item for item in pod["spec"]["containers"] if item["name"] == "model")
    offset = runtime["command"].index("--") + 1
    assert runtime["command"][offset:offset + 9] == ["python3", "/snapshot-source/working_directory_launcher.py", "--directory", "/opt/fs2/openfold3-preview2", "--uid", "10001", "--gid", "10001", "--"]
    assert runtime["command"][offset + 9:] == entrypoint
    assert "/snapshot-source/serving_launcher.py" not in runtime["command"]
    original = before["spec"]["template"]["spec"]["containers"][0]
    assert runtime["image"] == original["image"]
    assert runtime["resources"] == original["resources"]
    assert all(value in runtime["env"] for value in original["env"])
    assert next(value["value"] for value in runtime["env"] if value["name"] == "PATH").startswith("/opt/openfold3/.pixi/envs/openfold3-cuda12/bin:")
    assert next(item for item in runtime["volumeMounts"] if item["mountPath"] == "/model-cache") in original["volumeMounts"]
    assert "fsGroup" not in pod["spec"]["securityContext"]
    assert "fsGroupChangePolicy" not in pod["spec"]["securityContext"]
    assert "SYS_RESOURCE" in runtime["securityContext"]["capabilities"]["add"]
    assert source == before


def test_qualified_bundle_binds_original_native_command_and_three_restore_pairs():
    report_path = HERE / "snapshot-qualification.json"
    report = json.loads(report_path.read_bytes())
    bundle = ServingSnapshotBundle.model_validate_json((HERE / "snapshot-bundle.json").read_text())
    assert bundle.qualification_receipt_sha256 == hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert bundle.manifest_sha256 == report["bundle"]["manifest_sha256"]
    assert bundle.source_sha256 == report["compatibility"]["runtime_identity"]["snapshot_source_sha256"]
    assert bundle.runtime_command[0:2] == ["/bin/bash", "-ec"]
    assert bundle.address_python == "/opt/openfold3/.pixi/envs/openfold3-cuda12/bin/python3"
    assert bundle.supervisor_path.startswith(str(Path(bundle.address_python).parent) + ":")
    assert len(report["runs"]) == 6
    assert all(row["gpu_pod_deleted"] and len(row["requests"]) == 2 for row in report["runs"])
    assert "not unseen-input evidence" in report["semantic_scope"]
    assert not report["production_selectable"]
