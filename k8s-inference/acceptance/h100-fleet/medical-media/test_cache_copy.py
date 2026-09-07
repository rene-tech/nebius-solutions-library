import cache_copy
import json
from types import SimpleNamespace


def test_copy_is_cpu_only_source_readonly_and_destination_digest_verified():
    pod = cache_copy.manifest("source-volume-node")
    spec = pod["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "source-volume-node"}
    assert spec["restartPolicy"] == "Never" and spec["activeDeadlineSeconds"] == 7200
    source, destination = spec["volumes"]
    assert source["persistentVolumeClaim"] == {"claimName": cache_copy.SOURCE, "readOnly": True}
    assert destination["persistentVolumeClaim"] == {"claimName": cache_copy.DESTINATION}
    container = spec["containers"][0]
    assert "nvidia.com/gpu" not in container["resources"]["limits"]
    assert container["volumeMounts"][0]["readOnly"] is True
    command = container["command"][-1]
    assert cache_copy.CHECKPOINT_SHA256 in command
    assert command.index("sha256sum -c -") < command.index("mv /destination/")
    assert "test ! -e /destination/evo2_40b.pt" in command
    assert "rm " not in command


def test_pending_wait_for_first_consumer_destination_is_usable(monkeypatch, tmp_path):
    source = {"status": {"phase": "Bound"}, "spec": {"volumeName": "source-pv"}}
    destination = {"status": {"phase": "Pending"}, "spec": {"storageClassName": "csi-mounted-fs-path-sc"}}
    created = []

    def run(command, **kwargs):
        if "get" in command:
            value = source if cache_copy.SOURCE in command else destination
            return SimpleNamespace(stdout=json.dumps(value))
        value = json.loads(kwargs["input"])
        assert value["kind"] == "Pod"
        if "--dry-run=client" not in command:
            created.append(value)
        return SimpleNamespace(stdout="pod created")

    monkeypatch.setattr(cache_copy.subprocess, "run", run)
    monkeypatch.setattr("sys.argv", ["cache_copy.py", "--kubeconfig", "fixture", "--node", "source-node", "--output", str(tmp_path)])
    cache_copy.main()
    assert len(created) == 1
    assert created[0]["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"] is True
