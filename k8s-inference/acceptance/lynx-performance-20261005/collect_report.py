"""One explicit terminal Lynx selection into the existing phase/cost ledger.

No submissions, uploads or policy writes. Native probe rows remain separate
from public operations. Copies metadata only; original molecular payloads stay
at their retained immutable paths. Missing clocks remain null.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import UUID

from recipes import TPR_SHA256, save, sha
from validate_public import check as check_public

LEGACY = Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"
sys.path.insert(0, str(LEGACY))
from cost_report import build_report, write_summary_csv  # noqa: E402
from ledger import Ledger  # noqa: E402


def reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha(path), "bytes": path.stat().st_size}


def lifecycle_query(identifiers):
    ids = sorted({str(UUID(value)) for value in identifiers})
    if not 1 <= len(ids) <= 32:
        raise ValueError("Require one to 32 exact internal operation UUIDs")
    columns = ["s.operation_id", "s.subject_id", "s.attempt_id", "s.model_id", "r.rollup_id", "r.generated_at",
               "r.event_watermark", "r.events_sha256", "r.terminal", "r.outcome", "r.quota_reserved_gpu_seconds",
               "r.scheduler_occupied_gpu_seconds", "r.device_allocated_gpu_seconds", "r.active_gpu_seconds",
               "r.occupied_idle_gpu_seconds", "r.phase_gpu_seconds", "r.reconciled", "r.quality", "r.data_gaps"]
    return ("SELECT row_to_json(selected) FROM (SELECT " + ",".join(columns) +
            " FROM fs2_telemetry_subjects s LEFT JOIN fs2_reporting_lifecycle_latest r USING(subject_id) "
            "WHERE s.tenant_id='system' AND s.principal_id='qa' AND s.workload_kind='scientific_batch' "
            "AND s.model_id IN ('gromacs','gromacs-mpi') AND s.operation_id=ANY(ARRAY[" +
            ",".join("'" + value + "'::uuid" for value in ids) +
            "]) ORDER BY s.operation_id,s.attempt_id LIMIT 97) selected;")


def durable_rows(ids, context):
    query = lifecycle_query(ids)
    kube = ["kubectl", "--context", context, "--request-timeout=20s", "-n", "fs2-data"]
    cluster = json.loads(subprocess.check_output(
        [*kube, "get", "cluster.postgresql.cnpg.io", "fs2-control-db", "-o", "json"], timeout=25))
    raw = subprocess.check_output([
        *kube, "exec", cluster["status"]["currentPrimary"], "-c", "postgres", "--", "env",
        "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=10000",
        "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d",
        cluster["spec"]["bootstrap"]["initdb"]["database"], "-c", query], timeout=35)
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if len(rows) > 96 or any(row["operation_id"] not in ids for row in rows):
        raise ValueError("Durable lifecycle selection exceeded its explicit bound")
    return {"query": query, "query_sha256": hashlib.sha256(query.encode()).hexdigest(), "rows": rows,
            "missing_operations": sorted(ids - {row["operation_id"] for row in rows}),
            "captured_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
            "scope": "Application-observed PostgreSQL allocation clocks, not native integration or invoice clocks."}


def delivery_work(status, receipt, validation):
    """Measured server duration, never native work inferred from elapsed time."""
    operation = status["operation"]
    if operation["id"] != receipt["operation_id"]:
        raise ValueError("Status and verified receipt identify different operations")
    accepted, completed = operation.get("accepted_at"), operation.get("completed_at")
    seconds = None
    if accepted and completed:
        seconds = (datetime.fromisoformat(completed.replace("Z", "+00:00"))
                   - datetime.fromisoformat(accepted.replace("Z", "+00:00"))).total_seconds()
        if seconds <= 0:
            raise ValueError("Invalid server operation duration")
    repeats = validation["native"]["repeats"] if validation["native"]["status"] == "passed" else []
    ns = sum(row["simulated_ns"] for row in repeats) if repeats else None
    work = sum(row["steps"] * row["coordinates"]["atoms"] for row in repeats) if repeats else None
    return {"accepted_at": accepted, "completed_at": completed, "server_operation_seconds": seconds,
            "validated_simulated_ns": ns, "validated_particle_steps": work,
            "server_operation_ns_per_day": ns * 86400 / seconds if ns is not None and seconds else None,
            "server_operation_particle_steps_per_second": work / seconds if work is not None and seconds else None,
            "artifact_download_seconds": None,
            "scope": "Server accepted-to-completed includes queue, preparation, segment publication and analysis; "
                     "excludes input upload and client artifact download. Repeats restart the same original state."}


def lifecycle_comparisons(report, rows):
    """Apply the existing allocation-share rate to its matched durable clock."""
    result = []
    for operation in report["operations"]:
        op = operation["operation_id"]
        selected = [row for row in rows if row["operation_id"] == op and row.get("rollup_id")]
        expected = {row["attempt_id"] for row in report["attempts"] if row["operation_id"] == op}
        complete = (bool(expected) and len(selected) == len(expected)
                    and {row["attempt_id"] for row in selected} == expected
                    and all(row.get("terminal") and row.get("reconciled") for row in selected))
        occupied, cost = [], []
        for row in selected:
            allocation = [item for item in report["allocations"]
                          if item["operation_id"] == op and item["attempt_id"] == row["attempt_id"]]
            rates = [item["price"]["allocated_share_per_hour"] / item["gpu_count"]
                     for item in allocation if item["gpu_count"]
                     and item["price"]["allocated_share_per_hour"] is not None]
            rate = (rates[0] if rates and len(rates) == len(allocation)
                    and all(math.isclose(value, rates[0]) for value in rates) else None)
            gpu_seconds = row.get("scheduler_occupied_gpu_seconds")
            if gpu_seconds is not None and (not math.isfinite(gpu_seconds) or gpu_seconds < 0):
                raise ValueError("Invalid durable occupancy clock")
            occupied.append(gpu_seconds)
            cost.append(gpu_seconds * rate / 3600 if gpu_seconds is not None and rate is not None else None)
        total = sum(occupied) if complete and all(value is not None for value in occupied) else None
        dollars = sum(cost) if complete and all(value is not None for value in cost) else None
        result.append({"operation_id": op, "complete_terminal_attempt_coverage": complete,
                       "durable_scheduler_occupied_gpu_seconds": total,
                       "durable_allocation_share_cost_usd": dollars,
                       "observed_occupancy_gpu_seconds": operation["occupancy_gpu_seconds"],
                       "observed_allocation_share_cost_usd": operation["allocated_occupancy_cost"],
                       "quality": [row.get("quality") for row in selected],
                       "data_gaps": [row.get("data_gaps") for row in selected],
                       "scope": "Same dated per-GPU allocation-share prices as existing ledger; durable scheduler "
                                "clock is separate from observation bounds, native work, device utilization and invoice."})
    return result


def validated_work(index, case, recovered):
    """Fill existing interval measurements only for proven one-attempt sequences.

    The generic ledger intentionally leaves resumed intervals unknown. This
    exact recipe separately proves each segment starts at the preceding native
    checkpoint and binds its final command history to a remote commit.
    """
    proof = check_public(case)
    if proof["native"]["status"] != "passed":
        raise ValueError("Independent exact-input output validation failed")
    op = proof["operation_id"]
    attempts = index.db.execute("SELECT attempt_id,shard_id FROM attempts WHERE operation_id=?", (op,)).fetchall()
    if len(attempts) != 1:
        return {"operation_id": op, "status": "unknown", "reason": "multiple-attempt work requires explicit lineage"}
    attempt, shard = attempts[0]
    manifests = [json.loads(path.read_text()) for path in recovered.glob("*/attempt-*/checkpoint-*.json")]
    matching = [m for m in manifests if m["state"]["operation_id"] == op and m["state"]["job_id"] == shard]
    declaration = proof["remote_checkpoint_result_declaration"]
    native_generation = declaration["native_checkpoint_generation"]
    if (declaration["checkpoint_commit_scope"] != "remote-companion" or not matching
            or type(native_generation) is not int
            or type(declaration["committed_checkpoint_generation"]) is not int
            or declaration["committed_checkpoint_generation"] < native_generation):
        raise ValueError("Final native history lacks a matching remote checkpoint declaration")
    latest = max(matching, key=lambda m: m["state"]["generation"])
    if (latest["state"]["generation"] != native_generation
            or latest["state"]["recipe_sha256"] != proof["recipe_sha256"]
            or not any(f["path"] == "original.tpr" and f["sha256"] == TPR_SHA256 for f in latest["files"])):
        raise ValueError("Recovered final generation differs from verified native result")
    commands = latest["state"]["commands"]
    receipts = []
    for repeat in proof["native"]["repeats"]:
        name = f"repeat-{repeat['repeat']}"
        history = [c for c in commands if c["step_id"] == name]
        database = [{k: v for k, v in json.loads(row[0]).items()
                     if k not in ("ledger_source", "ledger_attempt_quality", "native_mdrun_counter_wall_seconds",
                                  "native_mdrun_counter_source")} for row in index.db.execute(
            "SELECT raw_json FROM commands WHERE operation_id=? AND attempt_id=? AND command_id=? ORDER BY finished_at",
            (op, attempt, name))]
        if history != database or len(history) != repeat["segments"]:
            raise ValueError("Remote checkpoint and existing command ledger disagree")
        ends = [c["checkpoint_step"] for c in history]
        if not ends or ends[-1] != repeat["steps"] or any(a >= b for a, b in zip([0, *ends], ends)):
            raise ValueError("Checkpoint sequence is not strictly increasing")
        receipts.append({"command_id": name, "interval": [0, ends[-1]], "segments": len(ends),
                         "repeated_durable_steps": 0, "source": "independent log continuity plus remote generation"})
        source = str(recovered) + "; native validation " + proof["native"]["native_result_sha256"]
        index.measure(op, attempt, shard, name, "durable_interval", [0, ends[-1]], "steps", "derived", source)
        index.measure(op, attempt, shard, name, "executed_steps", ends[-1], "steps", "derived", source)
    return {"operation_id": op, "status": "validated", "remote_generation": native_generation,
            "repeats": receipts, "native_output_validation": proof}


def freeze_telemetry(source, destination, ids, cutoff):
    destination.mkdir()
    pods, nodes, names, records = set(), set(), set(), []
    order = ["pods.jsonl", "pod-disappearance.jsonl", "container-samples.jsonl", "nodes.jsonl", "metrics.jsonl", "events.jsonl"]
    for name in order:
        path = source / name
        if not path.exists():
            records.append({"source": str(path), "status": "missing"})
            continue
        digest, selected, partial = hashlib.sha256(), 0, 0
        target = destination / name
        with path.open("rb") as stream, target.open("xb") as output:
            size = remaining = os.fstat(stream.fileno()).st_size
            while remaining:
                line = stream.readline(remaining)
                if not line:
                    raise ValueError("Observer file shrank during freeze")
                remaining -= len(line)
                digest.update(line)
                if not line.endswith(b"\n"):
                    partial += len(line)
                    continue
                row = json.loads(line)
                if datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) > cutoff:
                    continue
                if name == "pods.jsonl":
                    pod = row["pod"]
                    if pod["metadata"].get("labels", {}).get("fs2.nebius.ai/operation-id") not in ids:
                        continue
                    pods.add(pod["metadata"]["uid"])
                    names.add(pod["metadata"]["name"])
                    nodes.add(pod["spec"].get("nodeName"))
                elif name == "pod-disappearance.jsonl":
                    if row["pod_uid"] not in pods:
                        continue
                elif name == "container-samples.jsonl":
                    if row.get("pod_uid") not in pods:
                        continue
                else:
                    row["items"] = [item for item in row["items"] if
                                    (item["metadata"]["name"] in (nodes if name == "nodes.jsonl" else names))
                                    or (name == "events.jsonl" and item.get("involvedObject", {}).get("name") in names)]
                    if not row["items"]:
                        continue
                output.write((json.dumps(row) + "\n").encode())
                selected += 1
        records.append({"source": str(path), "source_prefix_bytes": size, "source_prefix_sha256": digest.hexdigest(),
                        "excluded_partial_bytes": partial, "selected_records": selected, "snapshot": reference(target)})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    args = parser.parse_args()
    os.umask(0o077)
    selection = json.loads(args.selection.read_text())
    if not 1 <= len(selection["public"]) <= 16:
        raise ValueError("Use a bounded explicit terminal campaign selection")
    cases, ids = [], set()
    for row in selection["public"]:
        cohort = Path(row["cohort"])
        receipts = list(cohort.glob("*/receipt.json"))
        if len(receipts) != 1 or row["interface"] not in ("REST", "MCP"):
            raise ValueError("This task submits one exact case per public cohort")
        case = receipts[0].parent
        receipt = json.loads(receipts[0].read_text())
        status = json.loads((case / "status.json").read_text())
        op = str(UUID(receipt["operation_id"]))
        if (op in ids or status["operation"]["status"] not in ("succeeded", "failed", "cancelled", "timed_out")
                or receipt["benchmark_identity"]["original_tpr_sha256"] != TPR_SHA256):
            raise ValueError("Duplicate, nonterminal or unrelated selected operation")
        ids.add(op)
        cases.append((row, case, receipt))
    cutoff = datetime.now(timezone.utc)
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "selection.json", selection)
    metadata = []
    for _, case, receipt in cases:
        for source in sorted(case.glob("*.json")):
            if source.stat().st_size > 32 * 1024**2:
                raise ValueError("Unexpectedly large case metadata")
            target = args.output / "metadata" / receipt["operation_id"] / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            metadata.append({"source": reference(source), "snapshot": reference(target)})
    frozen = freeze_telemetry(args.telemetry, args.output / "telemetry", ids, cutoff)
    index = Ledger(args.output / "measurements.sqlite")
    checks = []
    with index.db:
        for row, case, receipt in cases:
            index.cohort(Path(row["cohort"]), Path(row["recovered"]), row["interface"])
            if receipt.get("state") == "verified":
                checked = validated_work(index, case, Path(row["recovered"]) / case.name)
                if checked["status"] == "validated":
                    checked["delivery_work"] = delivery_work(json.loads((case / "status.json").read_text()),
                                                             receipt, checked["native_output_validation"])
                checks.append(checked)
        index.samples(args.output / "telemetry")
        index.correlate_allocations()
    integrity = index.db.execute("PRAGMA integrity_check").fetchall()
    foreign_keys = index.db.execute("PRAGMA foreign_key_check").fetchall()
    index.db.close()
    if integrity != [("ok",)] or foreign_keys:
        raise ValueError("New offline evidence index failed integrity checks")
    references = json.loads(args.references.read_text())
    save(args.output / "cost-references.json", references)
    report = build_report(args.output / "measurements.sqlite", references,
                          [Path(row["cohort"]) for row in selection["public"]], pricing_date="2026-10-05")
    report["public_comparisons"] = []
    report["comparison_scope"] = "Private exact TPR; no matched published benchmark or generic application-capacity claim."
    report["input_tpr_sha256"] = TPR_SHA256
    report["cutoff"] = cutoff.isoformat()
    report["native_probe_evidence"] = [{"receipt": reference(row["receipt"]),
                                        "data": json.loads(Path(row["receipt"]).read_text()),
                                        "fixture": reference(row["fixture"]), "notes": row.get("notes")}
                                       for row in selection.get("native", [])]
    save(args.output / "report.json", report)
    write_summary_csv(report, args.output / "operations.csv")
    save(args.output / "validated-work.json", checks)
    lifecycle = durable_rows(ids, args.context)
    save(args.output / "durable-lifecycle.json", {**lifecycle,
         "comparisons": lifecycle_comparisons(report, lifecycle["rows"]),
         "selected_report": reference(args.output / "report.json")})
    if any(reference(item["source"]["path"]) != item["source"] for item in metadata):
        raise ValueError("Selected terminal metadata changed during collection")
    save(args.output / "validation.json", {"cutoff": cutoff.isoformat(), "report": reference(args.output / "report.json"),
         "lifecycle": reference(args.output / "durable-lifecycle.json"), "metadata": metadata, "telemetry": frozen,
         "integrity": "ok", "foreign_key_errors": 0, "operation_ids": sorted(ids),
         "code": [reference(path) for path in (Path(__file__), LEGACY / "ledger.py", LEGACY / "cost_report.py")]})
    print(json.dumps({"report": reference(args.output / "report.json"), "coverage": report["coverage"]}))


if __name__ == "__main__":
    main()
