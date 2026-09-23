import hashlib
import importlib.util
import io
from pathlib import Path

import pytest


def probe():
    spec = importlib.util.spec_from_file_location("snapshot_probe", Path(__file__).with_name("snapshot_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python310_hash_compatibility():
    value = b"immutable captured CUDA code"
    assert probe().file_digest_compat(io.BytesIO(value), "sha256").hexdigest() == hashlib.sha256(value).hexdigest()


def test_paths_stay_in_owned_workspace(tmp_path):
    module = probe()
    assert module.relative_path(tmp_path, "work/md.log") == tmp_path / "work/md.log"
    for value in ("../customer", "/host", "work/../../other"):
        with pytest.raises(ValueError):
            module.relative_path(tmp_path, value)


def test_progress_is_native_logged_step(tmp_path):
    module = probe()
    (tmp_path / "md.log").write_text("  Step   Time\n   1000 2.0\n Step Time\n  2000 4.0\n")
    assert module.progress(tmp_path, {"progress_file": "md.log", "progress_pattern": r"Step\s+Time\s*\n\s*(\d+)"}) == 2000


def test_restore_refuses_same_or_missing_pod_identity():
    module = probe()
    assert module.same_worker({"pod_uid": "one"}, "one")
    assert module.same_worker({}, "two")
    assert module.same_worker({"pod_uid": "one"}, "")
    assert not module.same_worker({"pod_uid": "one"}, "two")


def test_progress_follows_closed_and_current_native_segments(tmp_path):
    (tmp_path / "part-1.log").write_text("1000 131072 1.0\n2000 131072 1.0\n")
    (tmp_path / "part-2.log").write_text("2000 131072 1.0\n3000 131072 1.0\n")
    assert probe().progress(tmp_path, {"progress_glob": "part-*.log", "progress_pattern": r"^(\d+)\s+131072"}) == 3000
    with pytest.raises(ValueError):
        probe().progress(tmp_path, {"progress_glob": "../customer/*", "progress_pattern": r"(\d+)"})


def test_manifest_has_one_gpu_and_no_host_access():
    spec = importlib.util.spec_from_file_location("renderer", Path(__file__).with_name("render_snapshot_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pod = module.render("test", "node", "runtime@sha256:abc", "tools@sha256:def", "source", "owned-pvc")
    assert pod["spec"]["activeDeadlineSeconds"] <= 3600
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert not any(pod["spec"].get(key) for key in ("hostPID", "hostIPC", "hostNetwork"))
    assert not any("hostPath" in volume for volume in pod["spec"]["volumes"])
    assert pod["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"
    with pytest.raises(ValueError):
        module.render("test", "node", "runtime:latest", "tools@sha256:def", "source", "owned-pvc")
    with pytest.raises(ValueError):
        module.render("test", "node", "runtime@sha256:abc", "tools@sha256:def", "source", "owned-pvc", 3601)
    network = module.render("test", "node", "runtime@sha256:abc", "tools@sha256:def", "source", "owned-pvc", network_configmap="owned-network")
    assert "xtables-nft-multi" in network["spec"]["initContainers"][0]["command"][2]
    assert not network["spec"].get("hostNetwork", False)
    assert not any(entry["name"] == "PATH" for entry in network["spec"]["containers"][0]["env"])


def test_network_cleanup_recognizes_only_own_loopback_criu_lock():
    module = probe()
    own = "-A INPUT -s 127.0.0.1/32 -d 127.0.0.1/32 -p tcp -m mark ! --mark 0xc114 -j DROP"
    assert module.own_tcp_lock_rule(own)
    assert not module.own_tcp_lock_rule(own.replace("0xc114", "0x1234"))
    assert not module.own_tcp_lock_rule(own.replace("127.0.0.1/32", "10.0.0.1/32"))
    assert not module.own_tcp_lock_rule("-P INPUT ACCEPT")


def test_network_tools_preserve_runtime_venv(monkeypatch):
    module = probe()
    monkeypatch.setenv("PATH", "/opt/fs2/venv/bin:/usr/bin")
    module.configure_network_tools({"pod_local_tcp_locking": True})
    assert module.os.environ["PATH"] == "/tools/usr/sbin:/opt/fs2/venv/bin:/usr/bin"
