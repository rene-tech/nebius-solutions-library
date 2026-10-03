"""Project verified actual-agent evidence into the existing offline MD ledger.

Read-only inputs; a fresh output directory is mandatory. Copy only metadata,
native result JSON and logs, never trajectories or original TPRs. The selected-
case verifier owns full input/delivery qualification. This adapter rechecks its
identity chain and native timing reports without submitting or replaying work.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from types import SimpleNamespace
from uuid import UUID

from verify_agent_case import (canonical, direct_delivery, normalize, runtime_canonical,
                               validate_plan, validate_recovery_identity)
from verify_agent_study import sha, verify_timing_report, workspace_path

SCHEMA = "fs2.gromacs-agent-ledger/v1"
MAX_BYTES = 64 * 1024**2


def bounded(data):
    if len(data) > MAX_BYTES:
        raise ValueError("Evidence exceeds the metadata/log size bound")
    return data


def load(path):
    with Path(path).open("rb") as stream:
        return json.loads(bounded(stream.read(MAX_BYTES + 1)))


def reference(path, data=None):
    if data is None:
        with Path(path).open("rb") as stream:
            data = bounded(stream.read(MAX_BYTES + 1))
    return {"path": str(path), "sha256": sha(data), "size_bytes": len(data)}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


class WorkspaceReader:
    """Only local mounted evidence, with optional bounded sudo read access."""
    def __init__(self, root, sudo=False):
        self.root, self.sudo = Path(root).resolve(), sudo

    def __call__(self, value):
        path = self.root / workspace_path(value)
        if self.sudo:
            resolved = Path(subprocess.check_output(
                ["sudo", "-n", "readlink", "-e", "--", str(path)], timeout=10,
                stdin=subprocess.DEVNULL, text=True).strip())
        else:
            resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Evidence symlink escapes the selected workspace")
        if self.sudo:
            data = subprocess.check_output(
                ["sudo", "-n", "head", "-c", str(MAX_BYTES + 1), "--", str(resolved)],
                timeout=15, stdin=subprocess.DEVNULL)
        else:
            with resolved.open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
        return bounded(data)


class Evidence:
    def __init__(self, reader):
        self.reader, self.data = reader, {}

    def fetch(self, path, expected=None):
        workspace_path(path)
        if path not in self.data:
            self.data[path] = bounded(self.reader(path))
        value = self.data[path]
        if expected and (sha(value) != expected["sha256"] or len(value) != expected["size_bytes"]):
            raise ValueError("Retained evidence differs from its verified hash/size")
        return value

    def json(self, path, expected=None):
        return json.loads(self.fetch(path, expected))


def tool_calls(messages):
    calls = []
    for message in messages:
        for part in message.get("content") or []:
            call = part.get("tool_call") if part.get("type") == "tool_call" else None
            if call:
                calls.append(call)
    return calls


def read_tool_output(raw):
    try:
        value = json.loads(raw)
        if isinstance(value, list) and len(value) == 1 and value[0].get("type") == "text":
            value = json.loads(value[0]["text"])
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def admitted_studies(messages):
    """Read exact accepted study IDs, not links or prose claiming completion."""
    result = set()
    for call in tool_calls(messages):
        value = read_tool_output(call.get("output"))
        if (call.get("name", "").startswith("run_scientific_workflow")
                and value.get("study_admission") == "accepted" and value.get("durable_study")):
            result.add(str(UUID(value["id"])))
    return result


def duration(value, scope):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
        raise ValueError("Invalid retained agent duration")
    return {"value": value, "unit": "seconds", "status": "measured" if value is not None else "unknown",
            "scope": scope}


def customer_outcome(proof, summary):
    """A successful native result cannot erase earlier customer-path friction."""
    observed = proof.get("observed_tool_errors")
    reported_clean = proof.get("customer_path_clean")
    if reported_clean is not None and type(reported_clean) is not bool:
        raise ValueError("Clean-path flag must be an explicit boolean or unknown")
    friction = any((observed, summary.get("errors"), summary.get("transport_warnings"),
                    summary.get("empty_answer"), summary.get("unfinished"), summary.get("watchdog_aborted")))
    return {"customer_path_clean": False if friction or reported_clean is False else reported_clean,
            "proof_reported_clean": reported_clean,
            "delivery_outcome": proof.get("delivery_outcome"), "observed_tool_errors": observed,
            "scope": "native/report verification is separate; absent clean-state evidence remains unknown"}


def agent_metadata(proof, summary, study, campaign, verification):
    path = proof.get("agent_path", "saved-study")
    if (summary["case_id"] != proof["case_id"]
            or study is not None and summary.get("output_directory") != study["output_directory"]
            or proof["client_image"] != campaign["client_image"]
            or campaign.get("tenant") != "system" or campaign.get("principal") != "qa"):
        raise ValueError("Agent case, workspace or internal client identity differs")
    start, end = (study or {}).get("created_at"), (study or {}).get("finished_at")
    elapsed = end - start if type(start) in (int, float) and type(end) in (int, float) else None
    fields = ("conversation_id", "model", "reasoning_effort", "cohort_id", "seeded_agent",
              "instructions_sha256", "core_instructions_sha256", "tool_calls", "tool_names",
              "errors", "transport_warnings", "empty_answer", "unfinished", "watchdog_aborted", "provider_usage")
    return {"schema": SCHEMA, "operation_id": proof["operation_id"], "case_id": proof["case_id"],
            "interface": "agent-skill-MCP", "agent_path": path, "study_id": proof["study_id"],
            "customer_outcome": customer_outcome(proof, summary), "recovery_binding": proof.get("recovery_binding"),
            "native_identity": {key: proof[key] for key in
                                ("input_sha256", "parameters_sha256", "native_results")},
            "chat": {**{key: summary.get(key) for key in fields}, "client_image": proof["client_image"],
                     "initial_response_scope": ("study admission; not a completed native report" if study is not None
                                                else "direct native/report delivery; no saved study"),
                     "elapsed_seconds": duration(summary.get("seconds"), "initial chat including tools"),
                     "tool_seconds": duration(summary.get("tool_seconds"), "harness sum of tool call durations; may overlap"),
                     "planning_seconds": duration(None, "not separately instrumented; do not subtract tool wall"),
                     "waiting_seconds": duration(None, "no independent active-wait timer"),
                     "delivery_seconds": duration(None, "no independent report/download duration timer"),
                     "token_cost_usd": None},
            "durable_delivery": {"kind": path, "study_created_at": start, "study_finished_at": end,
                                 "study_elapsed_seconds": duration(elapsed, "whole saved-study lifetime, not GPU or waiting time"
                                                                   if study is not None else "no saved study on direct-MCP path"),
                                 "verified": True, "native_report_verification": verification,
                                 "delivered_links": proof["report_verification"].get("delivered_links"),
                                 "scientific_convergence_claimed": False},
            "native_replayed": False,
            "accounting": "one original operation in ledger.py/cost_report.py; never cost chat wall as GPU wall"}


def collect_case(spec, reader, destination, campaign):
    proof_path, chat = Path(spec["proof"]), Path(spec["chat_directory"])
    if proof_path.name.endswith("-failure.json"):
        raise ValueError("Failure evidence is history, never a selected-case pass")
    proof, summary = load(proof_path), load(chat / "summary.json")
    warnings_file = chat / "transport-warnings.json"
    if warnings_file.exists():
        # Some harness revisions keep warnings in a separate durable sidecar.
        # Retain both observations; do not claim a clean path from native proof.
        warnings = load(warnings_file)
        if warnings:
            summary = {**summary, "transport_warnings": {
                "summary": summary.get("transport_warnings"), "sidecar": warnings}}
    if (proof.get("selected_case_verified") is not True or proof.get("native_replayed") is not False
            or proof.get("repeat_count") != 3 or proof.get("requested_steps_per_repeat") != 10000):
        raise ValueError("Require a verified selected case with three original repeats and no replay")
    operation = str(UUID(proof["operation_id"]))
    path = proof.get("agent_path", "saved-study")
    messages = load(chat / "messages.json")
    study, delivered = None, None
    source_paths = [proof_path, chat / "summary.json", chat / "messages.json"]
    if warnings_file.exists():
        source_paths.append(warnings_file)
    verification = proof["report_verification"]
    if verification.get("operation_id") != operation or verification.get("native_report_verified") is not True:
        raise ValueError("Admission-only or unrelated delivery does not qualify")
    if path == "saved-study":
        study_id = str(UUID(proof["study_id"]))
        if admitted_studies(messages) != {study_id}:
            raise ValueError("Chat does not bind exactly the selected admitted study")
        study = load(chat / "durable-terminal-state.json")
        source_paths.append(chat / "durable-terminal-state.json")
        if (study.get("id") != study_id or study.get("state") != "completed"
                or verification.get("study_id") != study_id
                or verification.get("durable_study_delivery_verified") is not True):
            raise ValueError("Admission-only or unrelated delivery does not qualify")
    elif path == "direct-batch-mcp":
        if (admitted_studies(messages) or any(proof.get(key) is not None for key in
                                           ("study_id", "model_step", "plan_identity"))
                or spec.get("frozen_record") is not None or proof.get("direct_delivery_verified") is not True):
            raise ValueError("Direct native delivery must not invent an admitted study")
        delivered = direct_delivery(tool_calls(messages), SimpleNamespace(read_tool_output=read_tool_output))
        if delivered["report_markdown"] not in summary.get("visible_text", ""):
            raise ValueError("Direct verified delivery was not present in the final reply")
        if not PurePosixPath(delivered["receipt_directory"]).is_relative_to(summary["output_directory"]):
            raise ValueError("Direct receipt is outside the selected chat output")
    else:
        raise ValueError("Unsupported actual-agent path")
    evidence = Evidence(reader)
    downloads = {}
    for row in verification["authenticated_downloads"]:
        if row["path"] in downloads and downloads[row["path"]] != row:
            raise ValueError("Conflicting authenticated download references")
        downloads[row["path"]] = row
    for row in downloads.values():
        suffix = PurePosixPath(row["path"]).suffix
        if suffix in {".json", ".md", ".csv", ".log"} or row["sha256"] in {
                item["source_sha256"] for item in proof["native_results"]}:
            evidence.fetch(row["path"], row)
    report_paths = [p for p in downloads if PurePosixPath(p).name == "native-timing-report.json"]
    if len(report_paths) != 1:
        raise ValueError("Require one unambiguous native timing report")
    report_path = PurePosixPath(report_paths[0])
    if delivered and any(str(report_path.with_name(name)) != delivered["files"][name] for name in
                         ("native-timing-report.json", "native-timings.csv", "native-timing-report.md")):
        raise ValueError("Direct proof differs from the actual delivered report")
    report = evidence.json(str(report_path))
    checked = verify_timing_report(report, evidence.fetch(str(report_path.with_name("native-timings.csv"))).decode(),
                                  evidence.fetch(str(report_path.with_name("native-timing-report.md"))).decode(), evidence.fetch)
    if any(checked[key] != verification.get(key) for key in checked):
        raise ValueError("Selected-case report verification changed")
    receipt_path = report["receipt_file"]
    if delivered and (PurePosixPath(receipt_path).parent != PurePosixPath(delivered["receipt_directory"])
                      or PurePosixPath(receipt_path).name not in {"receipt.json", "recovery-receipt.json"}):
        raise ValueError("Direct report differs from the actual delivered receipt")
    if receipt_path not in downloads:
        raise ValueError("Native receipt lacks authenticated hash evidence")
    native_root = PurePosixPath(receipt_path).parent
    receipt = evidence.json(receipt_path)
    submission = receipt
    request_root = native_root
    recovery = proof.get("recovery_binding")
    if PurePosixPath(receipt_path).name == "recovery-receipt.json":
        if (not delivered or not isinstance(recovery, dict) or recovery.get("same_operation_verified") is not True
                or recovery.get("operation_id") != operation or recovery.get("recovery_receipt_file") != receipt_path):
            raise ValueError("Recovery requires the verified original-operation binding")
        original_path = recovery["submission_receipt_file"]
        request_root = PurePosixPath(original_path).parent
        if (original_path not in downloads or PurePosixPath(original_path).name != "receipt.json"
                or not request_root.is_relative_to(summary["output_directory"])
                or downloads[original_path]["sha256"] != recovery["submission_receipt_sha256"]):
            raise ValueError("Original submission receipt lacks exact authenticated hash evidence")
        submission = evidence.json(original_path, downloads[original_path])
        validate_recovery_identity(submission, receipt)
    elif recovery is not None:
        raise ValueError("Recovery binding does not match a delivered recovery receipt")
    request = evidence.json(str(request_root / "request.json"))
    status = evidence.json(str(native_root / "status.json"))
    if status.get("operation", {}).get("id") != operation or status["operation"].get("status") != "succeeded":
        raise ValueError("Native status differs from the verified operation")
    if study is not None:
        record = evidence.json(spec["frozen_record"])
        plan = evidence.json(str(PurePosixPath(spec["frozen_record"]).with_name("plan.json")))
        step = next(row for row in plan["steps"] if row["id"] == proof["model_step"])
    else:
        parameter_paths = [p for p in downloads if PurePosixPath(p).name == "parameters-candidate.json"]
        if parameter_paths != ["/workspace/inputs/mpinat/" + proof["case_id"].removeprefix("mpinat-") + "/parameters-candidate.json"]:
            raise ValueError("Direct parameters are not the exact authenticated selected-case file")
        step = {"parameters": parameter_paths[0], "source": str(PurePosixPath(parameter_paths[0]).with_name("input.tar.gz"))}
    parameter_bytes = evidence.fetch(step["parameters"])
    parameters = json.loads(parameter_bytes)
    provenance = evidence.json(str(PurePosixPath(step["source"]).with_name("provenance.json")))
    if (sha(parameter_bytes) != proof["parameter_file_sha256"]
            or sha(canonical(parameters)) != proof["parameters_sha256"] or request["parameters"] != parameters
            or provenance["id"] != proof["case_id"].removeprefix("mpinat-")
            or provenance["bundle_sha256"] != proof["input_sha256"]):
        raise ValueError("Selected parameters or original input provenance differs")
    expected = {"source": {"path": step["source"], "sha256": proof["input_sha256"], "size_bytes": provenance["bundle_bytes"]},
                "parameter_path": step["parameters"], "parameter_file_sha256": sha(parameter_bytes),
                "parameter_size_bytes": len(parameter_bytes), "idempotency_key": request["idempotency_key"]}
    if study is not None:
        binding = validate_plan(expected, {"record": record, "plan": plan}, study)
        if any(binding[key] != proof[key] for key in binding):
            raise ValueError("Selected immutable study plan identity changed")
    identity = submission["identity"]
    if (identity.get("model_id") != "gromacs" or identity.get("source_sha256") != proof["input_sha256"]
            or identity.get("parameters_sha256") != proof["parameters_sha256"]
            or identity.get("idempotency_key") != request["idempotency_key"]
            or submission.get("operation_id") != operation
            or submission.get("request_descriptor", {}).get("compression") != "gzip"):
        raise ValueError("Native receipt identity differs from the selected input/parameters")
    natives = {row["source_sha256"]: row for row in proof["native_results"]}
    if set(natives) != set(checked["source_result_hashes"]):
        raise ValueError("Native source set differs from selected-case proof")
    artifacts = []
    for row in receipt["verified_artifacts"]:
        if row.get("semantic_type") == "gromacs-workflow-result/v1":
            native = evidence.json(row["path"], row)
            recipe = sha(runtime_canonical({"request": normalize(parameters), "job": native["job_id"], "image": native["engine_id"]}))
            original = next(f for f in native["files"] if f["path"] == "original.tpr")
            if (native["recipe_sha256"] != recipe or natives[row["sha256"]]["recipe_sha256"] != recipe
                    or original["sha256"] != provenance["tpr_sha256"] or original["size_bytes"] != provenance["tpr_bytes"]
                    or natives[row["sha256"]]["original_tpr_sha256"] != original["sha256"]):
                raise ValueError("Native recipe or original TPR identity changed")
            # Read native logs for atoms/dt and counter timers, but never the
            # original TPR/checkpoint/trajectory/energy binary payloads.
            hashes = {f["sha256"] for f in native["files"] if f["path"].endswith(".log")}
            for artifact in receipt["verified_artifacts"]:
                if artifact["sha256"] in hashes:
                    evidence.fetch(artifact["path"], artifact)
    for row in receipt["verified_artifacts"]:
        retained = row["path"] in evidence.data
        local = destination / "artifacts" / PurePosixPath(row["path"]).name
        artifacts.append({**row, "path": str(local), "source_workspace_path": row["path"],
                          "retention": "rehashed-local" if retained else "original-SDK-reference-only"})
        if retained:
            local.parent.mkdir(parents=True, exist_ok=True)
            with local.open("xb") as stream:
                stream.write(evidence.data[row["path"]])
    projected = {**receipt, "case": proof["case_id"].removeprefix("mpinat-"),
                 "input_sha256": proof["input_sha256"], "steps": 10000, "verified_artifacts": artifacts}
    for filename, value in (("receipt.json", projected), ("request.json", request), ("status.json", status),
                            ("provenance.json", provenance), ("selected-case-proof.json", proof)):
        save(destination / filename, value)
    metadata = agent_metadata(proof, summary, study, campaign, checked)
    metadata["source_evidence"] = [reference(p) for p in source_paths]
    metadata["history_evidence"] = [reference(Path(p)) for p in spec.get("history", [])]
    metadata["retention"] = {"source_artifacts": len(artifacts), "locally_rehashed_artifacts": sum(
        row["retention"] == "rehashed-local" for row in artifacts), "trajectories_copied": False,
        "original_input_rehashed_here": False, "original_input_scope": "selected-case proof and native input hashes; full verification retained upstream"}
    # Freeze small chat/proof/history metadata too. The original path remains a
    # source reference, not a promise that an active supervisor never updates it.
    for item in [*metadata["source_evidence"], *metadata["history_evidence"]]:
        with Path(item["path"]).open("rb") as stream:
            data = bounded(stream.read(MAX_BYTES + 1))
        if sha(data) != item["sha256"] or len(data) != item["size_bytes"]:
            raise ValueError("Local agent metadata changed during collection")
        target = destination / "evidence" / item["sha256"]
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
        item["retained_path"] = str(target)
    for path, value in evidence.data.items():
        target = destination / "evidence" / sha(value)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(value)
        metadata["source_evidence"].append({**reference(path, value), "retained_path": str(target)})
    save(destination / "agent-metadata.json", metadata)
    return metadata


def unique_operations(rows):
    """Recovery chats attach to the original operation, never another charge."""
    grouped = {}
    for row in rows:
        op = str(UUID(row["operation_id"]))
        if op in grouped and grouped[op]["native_identity"] != row["native_identity"]:
            raise ValueError("One operation cannot acquire different native physics during recovery")
        group = grouped.setdefault(op, {"operation_id": op, "native_identity": row["native_identity"], "agent_associations": []})
        cid = row["chat"]["conversation_id"]
        if any(r["chat"]["conversation_id"] == cid for r in group["agent_associations"]):
            raise ValueError("Duplicate chat association")
        group["agent_associations"].append(row)
    return list(grouped.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "workspace", "campaign-plan", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--sudo-read", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    manifest, campaign = load(args.manifest), load(args.campaign_plan)
    cases = manifest["cases"]
    if not 1 <= len(cases) <= 24:
        raise ValueError("Select one to 24 exact case proofs; do not scan a live campaign")
    names = [load(Path(row["proof"]))["case_id"] for row in cases]
    if len(set(names)) != len(names) or any(not re.fullmatch(r"mpinat-[a-z0-9-]+", name) for name in names):
        raise ValueError("Duplicate or invalid selected case names")
    args.output.mkdir(parents=True, exist_ok=False)
    rows = [collect_case(spec, WorkspaceReader(args.workspace, args.sudo_read),
                         args.output / "cohort-1" / name.removeprefix("mpinat-"), campaign)
            for spec, name in zip(cases, names)]
    if len(unique_operations(rows)) != len(rows):
        raise ValueError("Project one cohort per native operation; attach recovery metadata separately")
    result = {"schema": SCHEMA, "captured_at": datetime.now(timezone.utc).isoformat(),
              "manifest": reference(args.manifest), "campaign_plan": reference(args.campaign_plan),
              "operations": unique_operations(rows), "native_operations": len(rows),
              "read_only_sources": True, "simulation_submissions": 0}
    save(args.output / "agent-join.json", result)
    print(json.dumps({"path": str(args.output), "native_operations": len(rows),
                      "agent_join_sha256": reference(args.output / "agent-join.json")["sha256"]}))


if __name__ == "__main__":
    main()
