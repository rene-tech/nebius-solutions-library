"""Revalidation cannot erase execution failures or substitute incomplete data."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import revalidate_completed as m  # noqa: E402


def completed():
    failed = {"operation_id": m.OPERATION, "state": "failed", "errors": ["AttributeError"], "scenario": "recovery",
              "manual_recovery_performed": False, "model_revision": "revision",
              "tenant_policy": {"tenant_id": m.gate.TENANT, "principal_id": "owner"}}
    status = {"operation": {"id": m.OPERATION, "tenant_id": m.gate.TENANT, "principal_id": "owner", "model_id": "gromacs",
                            "model_revision": "revision", "status": "succeeded"}, "batch": {"result_published": True, "stages": []}}
    return failed, status


def test_exact_original_known_observer_failure_is_the_only_allowed_target():
    failed, status = completed()
    m.original_gate(failed, status, m.ORIGINAL_SHA256)
    with pytest.raises(m.gate.GateError, match="unapproved"):
        m.original_gate(failed, status, "different-original-receipt")


@pytest.mark.parametrize("change", [
    lambda value: value.update(errors=["native_semantic_gate_failed"]),
    lambda value: value.update(errors=["AttributeError", "cleanup_cancel_failed"]),
    lambda value: value.update(state="passed"),
    lambda value: value.update(manual_recovery_performed=True),
    lambda value: value.update(operation_id="92f3f692-0d68-45cf-a138-376100000001"),
])
def test_wrong_original_failure_or_execution_change_is_rejected(change):
    failed, status = completed()
    change(failed)
    with pytest.raises(m.gate.GateError):
        m.original_gate(failed, status, m.ORIGINAL_SHA256)


@pytest.mark.parametrize("change", [
    lambda value: value["operation"].update(status="running"),
    lambda value: value["operation"].update(status="failed"),
    lambda value: value["operation"].update(model_id="namd"),
    lambda value: value["operation"].update(principal_id="someone-else"),
    lambda value: value["operation"].update(model_revision="changed"),
    lambda value: value["batch"].update(result_published=False),
])
def test_nonterminal_or_changed_original_operation_cannot_be_requalified(change):
    failed, status = completed()
    change(status)
    with pytest.raises(m.gate.GateError):
        m.original_gate(failed, status, m.ORIGINAL_SHA256)


def test_observations_are_paired_without_editing_original_bytes(tmp_path):
    failed, status = completed()
    rows = [{"at": "2026-09-25T06:00:00Z", "jobs": [], "pods": [], "workloads": [], "nodes": []},
            {"at": "2026-09-25T06:00:01Z", "public_status": status}]
    path = tmp_path / "observations.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    before = path.read_bytes()
    assert m.paired_observations(path, failed)[0]["public_status"] == status
    assert path.read_bytes() == before
    path.write_text(json.dumps(rows[1]) + "\n")
    with pytest.raises(m.gate.GateError, match="unpaired"):
        m.paired_observations(path, failed)


def test_exact_old_null_exception_is_reproduced_and_correction_is_source_bound():
    spec = importlib.util.spec_from_file_location("prior_acceptance_fixtures", Path(__file__).with_name("test_run_acceptance.py"))
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    terminal, observations = fixtures.recovered_case()
    before = deepcopy(observations[0])
    before["public_status"]["batch"]["stages"][0]["attempts"][0]["recovery"] = None
    observations.insert(0, before)
    proof = m.correction_proof(terminal, observations)
    assert proof["commit"] == m.CORRECTION
    assert proof["before_source_sha256"] != proof["corrected_source_sha256"]
    with pytest.raises(m.gate.GateError, match="not_reproduced"):
        m.correction_proof(terminal, observations[1:])


def client_fixture(tmp_path):
    original = tmp_path / "original"
    (original / "customer").mkdir(parents=True)
    parameters = {"jobs": [{"id": "window-01"}]}
    m.gate.save(original / "parameters.json", parameters)
    artifact = original / "customer/output-00.artifact"
    artifact.write_bytes(b"complete native output")
    entry = {"artifact_id": "artifact-one", "sha256": m.gate.sha(artifact), "size_bytes": artifact.stat().st_size}
    manifest = original / "customer/output-manifest.json"
    m.gate.save(manifest, {"entries": [{"artifact": entry}]})
    identity = {"model_id": "gromacs", "endpoint": "https://example.test/mcp", "source_sha256": "source",
                "parameters_sha256": m.gate.digest(parameters)}
    client = {"state": "verified", "operation_id": m.OPERATION, "identity": identity,
              "output_manifest": {"sha256": m.gate.sha(manifest), "size_bytes": manifest.stat().st_size},
              "verified_artifacts": [{**entry, "publication": "verified-copy"}]}
    m.gate.save(original / "customer/receipt.json", client)
    failed = {"operation_id": m.OPERATION, "endpoint": identity["endpoint"],
              "fixture": {"input_sha256": "source", "derived_request_sha256": identity["parameters_sha256"]}}
    return original, failed, client


def test_complete_cli_manifest_and_each_native_artifact_are_rehashed(tmp_path):
    original, failed, _ = client_fixture(tmp_path)
    assert m.cli_artifacts(original, failed)["complete_artifacts_verified"] == 1
    (original / "customer/output-00.artifact").write_bytes(b"truncated")
    with pytest.raises(m.gate.GateError, match="artifact_missing_or_changed"):
        m.cli_artifacts(original, failed)


def test_incomplete_or_unverified_cli_receipt_is_rejected(tmp_path):
    original, failed, client = client_fixture(tmp_path)
    client["verified_artifacts"] = []
    (original / "customer/receipt.json").write_text(json.dumps(client))
    with pytest.raises(m.gate.GateError, match="inventory_incomplete"):
        m.cli_artifacts(original, failed)
    client["state"] = "partial"
    (original / "customer/receipt.json").write_text(json.dumps(client))
    with pytest.raises(m.gate.GateError, match="not_verified"):
        m.cli_artifacts(original, failed)


def duplicate_client_fixture(tmp_path):
    original, failed, client = client_fixture(tmp_path)
    manifest_path = original / "customer/output-manifest.json"
    entry = m.gate.read(manifest_path)["entries"][0]
    manifest_path.write_text(json.dumps({"entries": [{**entry, "name": name} for name in ("window-01.shared", "window-02.shared")]}))
    client["output_manifest"] = {"sha256": m.gate.sha(manifest_path), "size_bytes": manifest_path.stat().st_size}
    copied = client["verified_artifacts"][0]
    client["verified_artifacts"] = [{**copied, "name": name} for name in ("window-01.shared", "window-02.shared")]
    (original / "customer/output-01.artifact").write_bytes((original / "customer/output-00.artifact").read_bytes())
    (original / "customer/receipt.json").write_text(json.dumps(client))
    return original, failed, client


def test_content_addressed_duplicate_id_keeps_every_ordered_entry(tmp_path):
    original, failed, client = duplicate_client_fixture(tmp_path)
    assert len({row["artifact_id"] for row in client["verified_artifacts"]}) == 1
    assert m.cli_artifacts(original, failed)["complete_artifacts_verified"] == 2


@pytest.mark.parametrize("damage", ["missing_row", "wrong_bytes", "missing_file", "wrong_metadata", "wrong_order"])
def test_duplicate_id_does_not_hide_missing_changed_or_reordered_copy(tmp_path, damage):
    original, failed, client = duplicate_client_fixture(tmp_path)
    if damage == "missing_row":
        client["verified_artifacts"].pop()
    elif damage == "wrong_bytes":
        (original / "customer/output-01.artifact").write_bytes(b"changed bytes")
    elif damage == "missing_file":
        (original / "customer/output-01.artifact").rename(original / "customer/missing-copy.artifact")
    elif damage == "wrong_metadata":
        client["verified_artifacts"][1]["artifact_id"] = "other-artifact"
    else:
        client["verified_artifacts"].reverse()
    (original / "customer/receipt.json").write_text(json.dumps(client))
    with pytest.raises(m.gate.GateError):
        m.cli_artifacts(original, failed)


def test_changed_started_runtime_is_rejected():
    observation = [{"pods": [{"containers": [{"name": "scientific-stage", "started": True,
                    "image": "registry/gromacs@sha256:" + "a" * 64, "image_id": "registry/gromacs@sha256:" + "a" * 64}]}]}]
    with pytest.raises(m.gate.GateError, match="runtime"):
        m.runtime_proof(observation, "registry/gromacs@sha256:" + "b" * 64)
