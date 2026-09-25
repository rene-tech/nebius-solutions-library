#!/usr/bin/env python3
"""Read-only requalification of one completed run after a proven observer bug.

Never submits, uploads, cancels, deletes, deploys, materializes or edits original
evidence. Writes only a new caller-selected directory and a separate receipt.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

import run_acceptance as gate

OPERATION = "26d64c0b-2341-4072-a68f-c4704780d447"
ORIGINAL_SHA256 = "75a0395c88a253ebd1ef0457d237f9f8372b7f0257241e06f079a2aa5e39fdd9"
CORRECTION = "df6636f171a62f540cc55da7463a7a245055ae93"
RUNTIME_CORRECTION = "6101abd0a2643a44bbb437cdb4aacf74b4c0b879"
SOURCE = "k8s-inference/acceptance/admitted-pool-recovery-20260925/run_acceptance.py"
OLD = 'and r.get("recovery", {}).get("retry_not_before")]'
NEW = 'and (r.get("recovery") or {}).get("retry_not_before")]'


def original_gate(failed, terminal, original_sha):
    gate.require(original_sha == ORIGINAL_SHA256 and failed.get("operation_id") == OPERATION, "unapproved_original_receipt")
    gate.require(failed.get("state") == "failed" and failed.get("errors") == ["AttributeError"]
                 and failed.get("scenario") == "recovery" and failed.get("manual_recovery_performed") is False,
                 "original_failure_not_known_observer_error")
    terminal_gate(terminal, failed)


def terminal_gate(status, failed):
    gate.require(gate.accepted_operation(status, failed["tenant_policy"]) == failed["operation_id"], "terminal_operation_changed")
    gate.require(status["operation"]["status"] == "succeeded" and status["batch"]["result_published"] is True
                 and status["operation"]["model_revision"] == failed["model_revision"], "original_execution_not_successful")
    gate.require(all(row["resource_released"] for row in gate.attempts(status)), "terminal_attempt_not_released")


def paired_observations(path, failed):
    snapshots = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if "jobs" in row:
            gate.require(all(key in row for key in ("at", "pods", "workloads", "nodes")) and "public_status" not in row,
                         "observation_shape_changed")
            snapshots.append(row)
        else:
            gate.require(set(row) == {"at", "public_status"} and snapshots and "public_status" not in snapshots[-1],
                         "unpaired_public_observation")
            status = row["public_status"]
            gate.require(gate.accepted_operation(status, failed["tenant_policy"]) == failed["operation_id"], "observation_operation_changed")
            snapshots[-1]["public_status"] = status
    gate.require(snapshots and all("public_status" in row or not any(row[key] for key in ("jobs", "pods", "workloads"))
                                  for row in snapshots), "resource_observation_missing_public_pair")
    return snapshots


def correction_proof(terminal, observations):
    def git_source(revision):
        result = subprocess.run(["git", "show", revision + ":" + SOURCE], cwd=gate.ROOT.parent, capture_output=True, text=True, timeout=15)
        gate.require(result.returncode == 0, "observer_git_provenance_unavailable")
        return result.stdout

    before, after = git_source(CORRECTION + "^"), git_source(CORRECTION)
    gate.require(before.count(OLD) == 1 and before.replace(OLD, NEW, 1) == after, "correction_not_only_null_metadata_handler")
    functions = [node for node in ast.parse(after).body if isinstance(node, ast.FunctionDef) and node.name == "verify_recovery"]
    gate.require(len(functions) == 1 and ast.get_source_segment(after, functions[0]).strip() == inspect.getsource(gate.verify_recovery).strip(),
                 "current_recovery_verifier_changed")
    old_function = next(node for node in ast.parse(before).body if isinstance(node, ast.FunctionDef) and node.name == "verify_recovery")
    namespace = dict(vars(gate))
    exec(compile(ast.Module(body=[old_function], type_ignores=[]), "original_observer.py", "exec"), namespace)
    try:
        namespace["verify_recovery"](terminal, observations, "h100-1x")
    except AttributeError as error:
        gate.require(str(error) == "'NoneType' object has no attribute 'get'", "different_attribute_error_reproduced")
    else:
        raise gate.GateError("original_observer_error_not_reproduced")
    return {"commit": CORRECTION, "change": "Null-aware recovery metadata handler only; original exception reproduced",
            "before_source_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "corrected_source_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "current_source_sha256": gate.sha(Path(gate.__file__)), "revalidator_sha256": gate.sha(Path(__file__))}


def runtime_proof(observations, image):
    result = gate.verify_runtime(observations, image)
    source = subprocess.run(["git", "show", RUNTIME_CORRECTION + ":" + SOURCE], cwd=gate.ROOT.parent,
                            capture_output=True, text=True, timeout=15)
    gate.require(source.returncode == 0, "runtime_correction_provenance_unavailable")
    function = next(node for node in ast.parse(source.stdout).body if isinstance(node, ast.FunctionDef) and node.name == "verify_runtime")
    current = inspect.getsource(gate.verify_runtime).strip()
    gate.require(ast.get_source_segment(source.stdout, function).strip() == current, "runtime_verifier_source_changed")
    return {**result, "correction_commit": RUNTIME_CORRECTION, "verifier_sha256": hashlib.sha256(current.encode()).hexdigest(),
            "scope": "Actual CRI imageID digest, not CRI status.image/config ID; execution unchanged"}


def verify_file(path, expected_sha, expected_bytes=None):
    gate.require(path.is_file() and gate.sha(path) == expected_sha
                 and (expected_bytes is None or path.stat().st_size == expected_bytes), "retained_artifact_missing_or_changed")


def cli_artifacts(original, failed):
    client = gate.read(original / "customer/receipt.json")
    gate.require(client.get("state") == "verified" and client.get("operation_id") == failed["operation_id"], "customer_cli_not_verified")
    identity = client["identity"]
    gate.require(identity["model_id"] == "gromacs" and identity["endpoint"] == failed["endpoint"]
                 and identity["source_sha256"] == failed["fixture"]["input_sha256"]
                 and identity["parameters_sha256"] == failed["fixture"]["derived_request_sha256"], "customer_fixture_changed")
    gate.require(gate.digest(gate.read(original / "parameters.json")) == identity["parameters_sha256"], "customer_parameters_changed")
    manifest_path = original / "customer/output-manifest.json"
    manifest = client["output_manifest"]
    verify_file(manifest_path, manifest["sha256"], manifest["size_bytes"])
    entries = gate.read(manifest_path)["entries"]
    # Shared files may legitimately reuse a content-addressed artifact ID.
    # Preserve entry multiplicity/order and verify every downloaded copy.
    verified = client["verified_artifacts"]
    gate.require(len(verified) == len(entries) and entries, "customer_artifact_inventory_incomplete")
    for index, (entry, copied) in enumerate(zip(entries, verified)):
        artifact = entry["artifact"]
        gate.require(all(copied.get(key) == value for key, value in artifact.items())
                     and all(copied.get(key) == entry[key] for key in ("name", "semantic_type") if key in entry)
                     and copied.get("publication") == "verified-copy", "customer_artifact_unverified")
        verify_file(original / "customer" / f"output-{index:02d}.artifact", artifact["sha256"], artifact["size_bytes"])
    return {"receipt_sha256": gate.sha(original / "customer/receipt.json"), "manifest_sha256": manifest["sha256"], "complete_artifacts_verified": len(entries)}


def retained_native(original, failed, supplemental):
    native = supplemental["native"]
    verify_file(original / "native-semantic.json", native["semantic_sha256"])
    verify_file(original / "native-integrity/receipt.json", native["integrity_sha256"])
    semantic = gate.read(original / "native-semantic.json")
    integrity = gate.read(original / "native-integrity/receipt.json")
    windows = failed["fixture"]["windows"]
    gate.require(semantic["operation_id"] == failed["operation_id"] and {r["job_id"] for r in semantic["jobs"]} == set(windows), "native_semantic_identity_changed")
    gate.require(integrity["status"] == "passed" and integrity["frozen_reference_unchanged"] is True
                 and {r["window_id"] for r in integrity["windows"]} == set(windows), "native_integrity_not_passed")
    files = [*integrity["source_files"], *integrity["master_files"], *integrity["outputs"]]
    for window in integrity["windows"]:
        gate.require(window["status"] == "passed" and window["operation_id"] == failed["operation_id"]
                     and window["original_files_unchanged"] is True and window["trajectory_repair_performed"] is False,
                     "native_window_not_unchanged")
        files.extend(window["inputs"])
    for record in files:
        verify_file(Path(record["path"]), record["sha256"], record["bytes"])
    return {**native, "retained_inputs_outputs_and_validator_files_rehashed": len(files)}


def retained_accounting(original, failed, supplemental):
    path = original / "supplemental-accounting.json"
    verify_file(path, supplemental["accounting_sha256"])
    result = gate.read(path)
    gate.require(result["operation_id"] == failed["operation_id"] and result["tenant_id"] == failed["tenant_policy"]["tenant_id"]
                 and result["billing_claimed"] is False and result["payloads_exposed"] is False, "accounting_identity_changed")
    clocks = result["clock_reconciliation"]
    gate.require(set(clocks) == {"allocated", "active", "quota_reserved", "device_allocated"}
                 and all(value is True or value == "unavailable" for value in clocks.values()), "accounting_not_reconciled")
    gate.require(result["workloads"] and all(row["rollup"]["terminal"] is True for row in result["workloads"]), "accounting_not_terminal")
    return result


def run(args):
    import httpx

    os.umask(0o077)
    original = args.original.resolve()
    failed = gate.read(original / "receipt.json")
    original_sha = gate.sha(original / "receipt.json")
    terminal = gate.read(original / "terminal.json")
    original_gate(failed, terminal, original_sha)
    submission = gate.read(original / "customer/submission.json")
    gate.require(gate.accepted_operation(submission, failed["tenant_policy"], new=True) == OPERATION
                 and gate.read(original / "operation.json")["operation_id"] == OPERATION, "original_acceptance_changed")
    terminal_gate(gate.read(original / "customer/status.json"), failed)
    replay = gate.read(original / "replay.json")
    gate.require(replay["operation_id"] == OPERATION and replay["reused"] is True
                 and replay["workload_id"] == terminal["batch"]["workload_id"], "original_idempotent_replay_missing")
    observations = paired_observations(original / "observations.jsonl", failed)
    correction = correction_proof(terminal, observations)
    recovered = gate.verify_recovery(terminal, observations, "h100-1x")
    runtime = runtime_proof(observations, failed["release"]["runtime_image"])
    cli = cli_artifacts(original, failed)
    supplemental = gate.read(original / "supplemental-validation.json")
    gate.require(supplemental["original_failed_receipt_sha256"] == original_sha and supplemental["operation_id"] == OPERATION
                 and supplemental["original_failure_preserved"] is True and supplemental["release_unchanged"] is True
                 and supplemental["owned_resources_remaining"] == 0 and supplemental["recovery"] == recovered, "supplemental_validation_not_exact")
    native = retained_native(original, failed, supplemental)
    accounting = retained_accounting(original, failed, supplemental)
    key = gate.read(args.key_file)
    gate.require(key.get("disposable") is True and key["key"]["id"] == failed["key_id"]
                 and key["key"]["tenant_id"] == failed["tenant_policy"]["tenant_id"], "revalidation_key_changed")
    with httpx.Client(headers={"Authorization": "Bearer " + key["secret"]}, timeout=30, trust_env=False) as http:
        response = http.get(failed["endpoint"].removesuffix("/mcp") + "/v1/me")
        gate.require(response.status_code == 200 and gate.ordinary_policy(response.json(), key["key"]["tenant_id"]) == failed["tenant_policy"], "revalidation_policy_changed")
    current = gate.call(failed["endpoint"], key["secret"], "get_scientific_status", {"operation_id": OPERATION})
    terminal_gate(current, failed)
    gate.require(gate.verify_recovery(current, observations, "h100-1x") == recovered, "current_recovery_changed")
    kube = gate.Kube(args.kubeconfig, args.context)
    before = gate.read(original / "release-before.json")
    release = kube.release(failed["release"])
    gate.require(release == before and gate.digest(release) == failed["release_sha256"], "current_release_changed")
    injector = gate.observe_injector(kube, args.admission_injector_plan, failed["release"], failed["fixture"]["windows"])
    gate.require(injector == failed["injector"], "current_injector_changed")
    known = {row["workload_uid"] for row in gate.attempts(current) if row.get("workload_uid")}
    gate.require(not any(kube.owned(failed["tenant_policy"]["tenant_id"], OPERATION, known)), "original_resources_not_released")
    gate.require(gate.sha(original / "receipt.json") == original_sha, "original_failed_receipt_changed")
    output = args.output.resolve()
    gate.require(output != original and not output.exists(), "revalidation_output_must_be_new_directory")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    receipt = {**failed, "state": "passed", "errors": [], "finished_at": gate.now(), "customer_ready": False,
        "original_failure": {"receipt_sha256": original_sha, "reason": "AttributeError", "correction": correction,
                             "runtime_metadata_correction": {"commit": runtime["correction_commit"], "verifier_sha256": runtime["verifier_sha256"]},
                             "customer_execution_changed": False, "original_failure_receipt_preserved": True},
        "revalidation": {"kind": "read-only-observer-correction", "operation_resubmitted": False, "gpu_rerun": False,
            "observations_sha256": gate.sha(original / "observations.jsonl"), "supplemental_sha256": gate.sha(original / "supplemental-validation.json"),
            "cli": cli, "runtime": runtime, "fresh_public_terminal_sha256": gate.digest(current)},
        "native": native, "accounting_sha256": supplemental["accounting_sha256"], "recovered_attempts": recovered,
        "frozen_max_attempts_per_stage": accounting["retry"]["max_attempts_per_stage"], "idempotent_replay_verified": True,
        "owned_resources_remaining": 0, "retained_control_plane_ready": True, "manual_recovery_performed": False}
    gate.save(output / "receipt-revalidated.json", receipt)
    print(json.dumps({"state": "passed", "operation_id": OPERATION, "receipt": str(output / "receipt-revalidated.json"), "original_failure_preserved": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--admission-injector-plan", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    try:
        main()
    except gate.GateError as error:
        print(json.dumps({"state": "failed", "error": str(error)}))
        sys.exit(1)
