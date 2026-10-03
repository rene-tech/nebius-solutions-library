"""Read-only reconciliation of saved MPINAT agent evidence and follow-up plans.

Never submits/updates/cancels work. Optional live calls are system/qa operation
GETs. Old receipts/reports stay untouched; output must be a new evidence folder.
"""
import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess

import httpx

from native_timings import verify_native_timings

QA_PREFIX = "fs2_pat_56130b22ae09"
NATIVE_TYPE = "gromacs-workflow-result/v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def local_path(value, workspace):
    path = Path(value)
    if path.is_relative_to("/workspace"):
        path = workspace / path.relative_to("/workspace")
    if not path.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("Evidence artifact is outside the known workspace")
    return path


def case_of(path, workspace):
    parts = path.relative_to(workspace).parts
    if len(parts) >= 4 and parts[0] == "replays":
        return parts[3]
    return None


def files_without_history(root, names):
    for directory, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name != ".receipt-history"]
        for name in files:
            if name in names:
                yield Path(directory) / name


def parse_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def chat_record(directory):
    summary = load(directory / "summary.json", {})
    agent = load(directory / "agent.json", {})
    submission = load(directory / "submission.json", {})
    messages = load(directory / "messages.json", [])
    prompts = [item.get("text", "") for item in messages if item.get("isCreatedByUser")]
    studies, calls = [], []
    for message in messages:
        for part in message.get("content") or []:
            tool = part.get("tool_call") if part.get("type") == "tool_call" else None
            if not isinstance(tool, dict):
                continue
            calls.append(tool.get("name"))
            result = parse_json(tool.get("output"))
            if isinstance(result, dict) and result.get("durable_study"):
                studies.append({key: result.get(key) for key in (
                    "id", "state", "output_directory", "study_admission", "new_study_admitted")})
    return {"cohort": directory.parents[1].name, "case_id": directory.name,
            "evidence_directory": str(directory), "conversation_id": submission.get("conversationId"),
            "chat_submission_recorded": bool(submission), "saved_messages": len(messages),
            "prompt": "\n".join(prompts) or None,
            "model": agent.get("model"),
            "reasoning_effort": agent.get("model_parameters", {}).get("reasoning_effort"),
            "seeded_agent": summary.get("seeded_agent"),
            "tool_names": calls, "study_submissions": studies,
            "output_directory": summary.get("output_directory"),
            "visible_text": summary.get("visible_text"),
            "transport_warnings": load(directory / "transport-warnings.json", []),
            "message_file_sha256": sha(directory / "messages.json") if messages else None}


def missing_chat_messages(chats, container):
    """Read exact submitted conversations from the isolated QA MongoDB only."""
    if container != "fs2-default-release-mpinat-20261003":
        raise ValueError("This historical reconciliation permits only its recorded QA container")
    ids = [row["conversation_id"] for row in chats if row["conversation_id"] and not row["saved_messages"]]
    if not ids:
        return {}
    javascript = """
const mongoose = require('mongoose');
(async () => {
  if (!process.env.MONGO_URI) throw new Error('QA Mongo URI absent');
  await mongoose.connect(process.env.MONGO_URI);
  const ids = JSON.parse(process.argv[1]);
  const messages = await mongoose.connection.db.collection('messages').find(
    {conversationId: {$in: ids}},
    {projection: {_id: 0, conversationId: 1, messageId: 1, isCreatedByUser: 1,
                  text: 1, content: 1, createdAt: 1, updatedAt: 1, error: 1, unfinished: 1}}
  ).sort({createdAt: 1}).toArray();
  console.log(JSON.stringify(messages));
  await mongoose.disconnect();
})().catch(error => {console.error(error.name); process.exit(1);});
"""
    process = subprocess.run(["docker", "exec", "-w", "/app", container, "node", "-e", javascript, json.dumps(ids)],
                             capture_output=True, text=True, check=True, timeout=30)
    messages = json.loads(process.stdout)
    return {cid: [message for message in messages if message.get("conversationId") == cid] for cid in ids}


def saved_receipts(workspace):
    rows = []
    for path in files_without_history(workspace / "replays", {"receipt.json", "recovery-receipt.json"}):
        value = load(path, {})
        if not value.get("operation_id"):
            continue
        request = load(path.parent / "request.json")
        identity = value.get("identity", {})
        row = {"case_id": case_of(path, workspace), "operation_id": value["operation_id"],
               "receipt_file": str(path), "receipt_sha256": sha(path),
               "receipt_state": value.get("state"), "idempotency_key": identity.get("idempotency_key"),
               "source_sha256": identity.get("source_sha256"),
               "parameters_sha256": identity.get("parameters_sha256"),
               "request_file": str(path.parent / "request.json") if request else None,
               "native_timings": None, "native_results": [], "errors": []}
        translated = copy.deepcopy(value)
        for artifact in translated.get("verified_artifacts", []):
            artifact["path"] = str(local_path(artifact["path"], workspace))
            if artifact.get("semantic_type") == NATIVE_TYPE:
                native = load(Path(artifact["path"]), {})
                row["native_results"].append({"path": artifact["path"],
                    "sha256": artifact["sha256"], "job_id": native.get("job_id"),
                    "status": native.get("status"), "operation_id": native.get("operation_id")})
        if request and translated.get("verified_artifacts"):
            try:
                row["native_timings"] = verify_native_timings(request, translated)
            except (OSError, ValueError, KeyError) as error:
                row["errors"].append(str(error))
        rows.append(row)
    # Recovery receipts sometimes omit the original request; validate against
    # the preserved original receipt of the same operation, never another run.
    requests = {row["operation_id"]: row["request_file"] for row in rows if row["request_file"]}
    for row in rows:
        if not row["native_timings"] and row["native_results"] and row["operation_id"] in requests:
            receipt = load(Path(row["receipt_file"]))
            for artifact in receipt.get("verified_artifacts", []):
                artifact["path"] = str(local_path(artifact["path"], workspace))
            try:
                row["native_timings"] = verify_native_timings(load(Path(requests[row["operation_id"]])), receipt)
            except (OSError, ValueError, KeyError) as error:
                row["errors"].append(str(error))
    return rows


def study_records(workspace):
    rows = []
    for path in (workspace / ".scientific-studies").glob("*/*/receipt.json"):
        value = load(path)
        output = local_path(value["output_directory"], workspace)
        rows.append({"case_id": case_of(output, workspace), "study_id": value.get("id"),
                     "state": value.get("state"), "phase": value.get("phase"),
                     "error": value.get("error"), "output_directory": value["output_directory"],
                     "receipt_file": str(path), "receipt_sha256": sha(path),
                     "completed_steps": value.get("completed_steps"),
                     "steps": {name: {key: step.get(key) for key in ("state", "operation_id", "error")}
                               for name, step in value.get("steps", {}).items()}})
    return rows


def report_records(workspace):
    names = {"timing-summary.csv", "metrics.csv", "timings.csv", "timing-summary.md", "report.md"}
    rows = []
    for path in files_without_history(workspace / "replays", names):
        text = path.read_text()
        row = {"case_id": case_of(path, workspace), "path": str(path),
               "sha256": sha(path), "size_bytes": path.stat().st_size}
        if path.suffix == ".csv":
            rows_csv = list(csv.DictReader(io.StringIO(text)))
            row.update(csv_data_rows=len(rows_csv), rows=rows_csv,
                       empty_timing_table=len(rows_csv) == 0)
        else:
            row.update(text=text, unsupported_zero_claim=bool(re.search(
                r"no science ran|before any mdrun|convert-tpr never ran|zero MD steps", text, re.I)))
        rows.append(row)
    return rows


def observe_operations(operation_ids, qa_env, api_base=None):
    values = dict(line.split("=", 1) for line in qa_env.read_text().splitlines() if "=" in line)
    key = values["SCIENTIFIC_MODELS_API_KEY"].strip("\"'")
    if not key.startswith(QA_PREFIX):
        raise ValueError("Only the existing system/qa identity is permitted")
    endpoint = api_base or values.get("SCIENTIFIC_MODELS_MCP_URL")
    if not endpoint:
        raise ValueError("Supply the verified API origin when the saved environment omits the inherited MCP URL")
    base = endpoint.strip("\"'").removesuffix("/mcp").removesuffix("/mcp/")
    result = {}
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + key}, timeout=30, trust_env=False) as client:
        for operation_id in sorted(operation_ids):
            row = {"operation_id": operation_id, "observed_at": now(), "method": "GET"}
            try:
                response = client.get(f"/v1/operations/{operation_id}")
                row["http_status"] = response.status_code
                if response.status_code != 200:
                    row["status"] = "unknown"
                else:
                    value = response.json()
                    operation, batch = value.get("operation", value), value.get("batch", {})
                    row.update(status=operation.get("status", "unknown"),
                               batch_status=batch.get("status"), result_published=batch.get("result_published"),
                               failure_code=batch.get("failure_code"), response_sha256=hashlib.sha256(response.content).hexdigest(),
                               attempts=[{key: attempt.get(key) for key in (
                                   "attempt_id", "outcome", "resource_released", "scheduling_admission", "failure_code")}
                                   for stage in batch.get("stages", []) for attempt in stage.get("attempts", [])])
            except httpx.HTTPError as error:
                row.update(status="unknown", observation_error=type(error).__name__)
            result[operation_id] = row
    return result


def coverage(suite, chats, receipts, studies, reports, observations):
    result = []
    for case in [row["id"] for row in suite["cases"]] + ["expected-native-failure"]:
        case_id = case if case == "expected-native-failure" else "mpinat-" + case
        matched = [row for row in receipts if row["case_id"] == case_id]
        ids = sorted({row["operation_id"] for row in matched})
        native = [row["native_timings"] for row in matched if row.get("native_timings")]
        metrics = [row for row in reports if row["case_id"] == case_id and "csv_data_rows" in row]
        result.append({"case_id": case_id, "scientific_case": case != "expected-native-failure",
            "chats": [row for row in chats if row["case_id"] == case_id],
            "operation_ids": ids, "observations": [observations.get(op, {"operation_id": op, "status": "unknown"}) for op in ids],
            "native_benchmark_complete": any(row["benchmark_complete"] for row in native),
            "native_timing_rows": sum(len(row["repeats"]) for row in native if row["benchmark_complete"]),
            "study_states": [{"id": row["study_id"], "state": row["state"]} for row in studies if row["case_id"] == case_id],
            "report_csv_rows": sum(row["csv_data_rows"] for row in metrics),
            "empty_report_files": [row["path"] for row in metrics if row["empty_timing_table"]],
            "customer_report_verified": False,
            "followup": "recover-and-report-existing" if any(row["benchmark_complete"] for row in native) else
                        "resolve-existing-operation" if any(observations.get(op, {}).get("status", "unknown")
                                                            not in {"failed", "cancelled", "expired", "preempted"} for op in ids) else
                        "new-qualified-execution-needed"})
    return result


def followups(rows, receipts, existing_manifest):
    recover, remaining, unresolved = [], [], []
    for row in rows:
        case = row["case_id"]
        if not row["scientific_case"]:
            continue
        if row["followup"] == "recover-and-report-existing":
            receipt = next(item for item in receipts if item["case_id"] == case and
                           (item.get("native_timings") or {}).get("benchmark_complete"))
            op = receipt["operation_id"]
            prompt = (f"Recover the completed Scientific AI GROMACS operation {op} for the public {case.removeprefix('mpinat-')} "
                "benchmark. Do not submit, resubmit or cancel any scientific operation; this is retrieval and analysis only, "
                "with no GPU replay. Use the installed GROMACS skill and verified file client, retrieve the existing operation "
                "into a new directory under {output}, and follow the native runtime-result references rather than treating the "
                "platform result envelope as native timings. Produce a real CSV/Markdown report for all three 10,000-step "
                "timing repeats: recorded ns/day and wall time, source log/hash, requested/executed/durably completed steps "
                "where evidenced, and useful verified result links. Keep unmeasured quantities explicitly unknown. Check the "
                "requested repeat set and actual native commands; an empty timing table is incomplete, not success. "
                "These are identical-start performance repeats including initialization/tuning, not independent scientific "
                "replicas. Preserve previous files and state; if an artifact or metric cannot be resolved, identify that gap.")
            recover.append({"case_id": "recover-" + case, "source_case_id": case,
                            "operation_id": op, "new_scientific_operations_allowed": 0, "prompt": prompt})
        elif row["followup"] == "resolve-existing-operation":
            unresolved.append({"case_id": case, "operation_ids": row["operation_ids"], "reason": "Do not duplicate unresolved admission"})
        else:
            original = existing_manifest[case]
            prompt = original["prompt"].replace("parameters-b.json", "parameters-candidate.json")
            prompt = prompt.replace("fs2-mpinat-agent-b-", "fs2-mpinat-candidate-73a3340a-")
            remaining.append({"case_id": case, "previous_operation_ids": row["operation_ids"], "prompt": prompt,
                              "requires_staging_candidate_parameters": True})
    metadata = {"model": "moonshotai/Kimi-K3", "reasoning_effort": "high", "use_seeded_agent": True,
                "candidate_image": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:73a3340a2c021641b1e4b8ffef2298bf788d5820aede746d07fe18e94a8843c0",
                "candidate_source": "0700a6fd81bcae77be229d33e79c9fbb097dbe7f",
                "tenant": "system", "principal": "qa", "execution_started": False}
    return {"recovery": {**metadata, "cases": recover}, "remaining": {**metadata, "cases": remaining}, "unresolved": unresolved}


def markdown(rows):
    lines = ["# Saved agent MPINAT coverage", "", "Historical client image/overlays, not candidate qualification.", "",
             "| Case | Agent chats submitted | Distinct operations | Live states | Native timing rows | Report CSV rows | Follow-up |",
             "| --- | ---: | ---: | --- | ---: | ---: | --- |"]
    for row in rows:
        lines.append("| " + " | ".join(map(str, [row["case_id"],
            sum(item["chat_submission_recorded"] for item in row["chats"]), len(row["operation_ids"]),
            ", ".join(item["status"] for item in row["observations"]) or "not submitted",
            row["native_timing_rows"], row["report_csv_rows"], row["followup"]])) + " |")
    lines += ["", "Zero report rows are not zero computation. No row is a customer-ready claim.",
              "Full operation IDs, prompts, study/receipt paths and hashes are in reconciliation.json."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qa-env", type=Path, help="Enables read-only operation GET reconciliation with existing system/qa")
    parser.add_argument("--api-base", help="Verified API origin when inherited URL is absent from the saved environment")
    parser.add_argument("--container", help="Read missing saved conversations from the exact historical QA container")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    chats = [chat_record(path) for cohort in ("agent-a", "agent-b", "agent-c")
             for path in sorted((args.evidence / cohort).glob("*/*")) if path.is_dir()]
    if args.container:
        recovered = missing_chat_messages(chats, args.container)
        for row in chats:
            messages = recovered.get(row["conversation_id"])
            if messages:
                path = args.output / "recovered-chats" / (row["conversation_id"] + ".json")
                save(path, messages)
                row.update(saved_messages=len(messages), recovered_message_file=str(path),
                           prompt="\n".join(message.get("text", "") for message in messages if message.get("isCreatedByUser")),
                           message_file_sha256=sha(path))
    receipts, studies, reports = saved_receipts(args.workspace), study_records(args.workspace), report_records(args.workspace)
    observed = observe_operations({row["operation_id"] for row in receipts}, args.qa_env, args.api_base) if args.qa_env else {}
    suite = load(args.evidence / "fixtures/suite.json")
    rows = coverage(suite, chats, receipts, studies, reports, observed)
    followup = followups(rows, receipts, {row["case_id"]: row for row in load(args.evidence / "agent-b-manifest.json")["cases"]})
    result = {"schema": "fs2.gromacs-agent-reconciliation/v1", "observed_at": now(),
              "scope": "Historical isolated QA client, read-only; not candidate or Serverless/browser qualification",
              "chats": chats, "receipts": receipts, "studies": studies, "reports": reports,
              "operations": observed, "coverage": rows, "unresolved": followup["unresolved"]}
    save(args.output / "reconciliation.json", result)
    (args.output / "COVERAGE.md").write_text(markdown(rows))
    save(args.output / "recover-existing-manifest.json", followup["recovery"])
    save(args.output / "remaining-cases-manifest.json", followup["remaining"])
    for case in followup["remaining"]["cases"]:
        name = case["case_id"].removeprefix("mpinat-")
        parameters = load(args.workspace / "inputs/mpinat" / name / "parameters-b.json")
        parameters["output_prefix"] = "runs/fs2-mpinat-candidate-73a3340a-20261003/" + name
        if name in {"benchpep", "benchpep-h"}:
            parameters["max_output_bytes"] = 24 * 1024**3
        save(args.output / "candidate-parameters" / name / "parameters-candidate.json", parameters)
    print(json.dumps({"output": str(args.output), "chats_with_submission": sum(row["chat_submission_recorded"] for row in chats),
                      "distinct_operations": len({row["operation_id"] for row in receipts}),
                      "recover_existing": len(followup["recovery"]["cases"]),
                      "new_execution_needed": len(followup["remaining"]["cases"]), "unresolved": len(followup["unresolved"]),
                      "scientific_submissions": 0}))


if __name__ == "__main__":
    main()
