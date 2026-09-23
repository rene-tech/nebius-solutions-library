import argparse
import hashlib
import json

import pytest

from recover_image_customer_results import command, run, save_private, verify_downloads


OPERATION = "80fb3f64-d199-4884-b8ec-d503d38c69d7"
IMAGE = "registry.example/client@sha256:" + "a" * 64


def put(path, value):
    path.write_text(json.dumps(value))


def mutate(path, action):
    value = json.loads(path.read_text())
    action(value)
    put(path, value)


@pytest.fixture
def downloads(tmp_path):
    output, previous = tmp_path / "new", tmp_path / "previous"
    output.mkdir()
    previous.mkdir()
    raw = b"retained native scientific output\n"
    reference = {"artifact_id": "artifact-1", "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    (output / "output-00.artifact").write_bytes(raw)
    manifest = {"entries": [{"name": "native", "artifact": reference}]}
    for folder in (output, previous):
        put(folder / "output-manifest.json", manifest)
    raw_manifest = (output / "output-manifest.json").read_bytes()
    manifest_ref = {"artifact_id": "manifest-1", "size_bytes": len(raw_manifest), "sha256": hashlib.sha256(raw_manifest).hexdigest()}
    identity = {"endpoint": "https://platform.example/mcp", "caller_fingerprint": hashlib.sha256(b"ordinary-test-key").hexdigest()}
    original = {"operation_id": OPERATION, "state": "verified", "identity": identity}
    put(previous / "receipt.json", original)
    def download(ref):
        return {**ref, "publication": "verified-copy", "transfer_attempts": 1}
    put(output / "recovery-receipt.json", {**original, "output_manifest": download(manifest_ref), "verified_artifacts": [download(reference)]})
    put(output / "result.json", {"operation_id": OPERATION, "terminal_status": "succeeded", "output_manifest": manifest_ref})
    put(output / "status.json", {"operation": {"id": OPERATION, "model_id": "amber", "status": "succeeded"}, "batch": {"result_published": True}})
    return output, previous


def test_fresh_readback_is_independently_hashed_not_a_new_simulation(downloads):
    summary = verify_downloads(*downloads, OPERATION)
    assert summary["verified_artifacts"] == 1
    assert summary["fresh_content_downloads_including_manifest"] == 2
    assert summary["transport_attempts_including_manifest"] == 2
    assert not summary["inference_submitted"] and not summary["scientific_simulations_rerun"]
    assert "caller_fingerprint" not in json.dumps(summary)


@pytest.mark.parametrize("filename,action,match", [
    ("recovery-receipt.json", lambda r: r.update(operation_id="another"), "same completed operation"),
    ("recovery-receipt.json", lambda r: r["identity"].update(caller_fingerprint="another"), "owner or endpoint"),
    ("recovery-receipt.json", lambda r: r["identity"].update(endpoint="https://other.example/mcp"), "owner or endpoint"),
    ("recovery-receipt.json", lambda r: r["verified_artifacts"][0].update(publication="verified-existing"), "reused local artifacts"),
    ("recovery-receipt.json", lambda r: r["verified_artifacts"][0].update(sha256="0" * 64), "download identity"),
    ("recovery-receipt.json", lambda r: r.update(verified_artifacts=[]), "complete manifest"),
    ("status.json", lambda r: r["operation"].update(status="running"), "published successful"),
    ("result.json", lambda r: r.update(terminal_status="failed"), "published successful"),
    ("output-manifest.json", lambda r: r.update(changed=True), "fresh manifest"),
])
def test_rejects_changed_identity_or_stale_downloads(downloads, filename, action, match):
    mutate(downloads[0] / filename, action)
    with pytest.raises(ValueError, match=match):
        verify_downloads(*downloads, OPERATION)


def test_rejects_independent_artifact_corruption(downloads):
    (downloads[0] / "output-00.artifact").write_bytes(b"changed")
    with pytest.raises(ValueError, match="independent size/SHA"):
        verify_downloads(*downloads, OPERATION)


def test_recovery_invocation_has_no_admission_or_input_parameters(tmp_path):
    argv = command(IMAGE, OPERATION, tmp_path / "new")
    assert argv[-4:] == ["--recover-operation-id", OPERATION, "--output", "/recovery"]
    assert not any(flag in argv for flag in ("--source", "--parameters", "--tool", "--operation", "--gpus"))
    assert "SCIENTIFIC_MODELS_API_KEY" in argv  # Name only, never its value.
    with pytest.raises(ValueError, match="pin the exact"):
        command("registry.example/client:latest", OPERATION, tmp_path)


def test_wrong_owner_rejected_before_docker_or_new_directory(downloads, tmp_path, monkeypatch):
    key = tmp_path / "key.json"
    put(key, {"secret": "wrong-key"})
    args = argparse.Namespace(image=IMAGE, operation_id=OPERATION, output=tmp_path / "unused",
                              previous_receipt=downloads[1], key_file=key, mcp_url="https://platform.example/mcp")
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: pytest.fail("no Docker or network work expected"))
    with pytest.raises(ValueError, match="same ordinary owner"):
        run(args)
    assert not args.output.exists()


def test_private_records_are_exclusive_and_mode_600(tmp_path):
    path = tmp_path / "receipt.json"
    save_private(path, {"status": "passed"})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save_private(path, {"status": "changed"})
