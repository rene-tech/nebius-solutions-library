"""Rebuild a queryable SQLite evidence ledger; never infer success from timing.

The public platform's PostgreSQL is authoritative. This is an offline analytical
index of immutable evidence, not a billing database or an admission controller.
Unmeasured quantities remain null with an explicit reason/source.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def union_steps(intervals):
    """Length of (start, end] union for ONE logical replica; no retry billing."""
    result, end = 0, None
    for start, stop in sorted(intervals):
        if start < 0 or stop < start:
            raise ValueError("invalid step interval")
        result += max(0, stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return result


def protocol(text):
    fields = dict(re.findall(r"^\s+([a-zA-Z][\w-]*)\s*=\s*(.*?)\s*$", text, re.M))
    atoms = re.search(r"There are:\s+(\d+)\s+Atoms", text)
    return {"particle_count": int(atoms[1]) if atoms else None,
            "timestep_ps": float(fields["dt"]) if "dt" in fields else None,
            "initial_step": int(fields["init-step"]) if "init-step" in fields else None,
            "force_field_name": None, "force_field_name_quality": "unknown; executable parameters retained in original TPR",
            "native_input_parameters": fields}


def native_counter_seconds(text):
    """GROMACS's native Wall t counter, not integration-only or GPU time.

    Multiple tables in one command log are ambiguous (e.g. concatenated runs).
    Do not select the fastest, sum rank counters, or subtract this from process
    wall and call the difference initialization.
    """
    number = r"[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?"
    rows = re.findall(r"Core t \(s\)\s+Wall t \(s\)\s+\(%\)\s*\n\s*Time:\s+(" + number +
                      r")\s+(" + number + r")\s+(" + number + r")", text)
    if len(rows) != 1:
        return None
    seconds = float(rows[0][1])
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def frozen_stage_modes(root, operation_id, status):
    path = root / "frozen-plan.json"
    document = load(path)
    if document is None:
        return {}
    model = status.get("operation", {}).get("model_id") or status.get("batch", {}).get("model_id")
    if (document.get("operation_id") != operation_id or document.get("tenant_id") != "system"
            or document.get("model_id") not in {"gromacs", "gromacs-mpi"}
            or model is not None and document["model_id"] != model
            or document.get("source") != "postgresql-admitted-plan"):
        raise ValueError("Frozen plan identity/source does not match this system operation")
    return {stage["stage_id"]: stage["mode"] for stage in document["plan"]["stages"]}


def native_shard(public_shard, mode, jobs):
    if mode == "gang-jobset":
        if public_shard is not None or len(jobs) != 1:
            raise ValueError("TRUE_GANG must have one native job and a null public shard")
        return jobs[0]["id"]
    if public_shard is None:
        raise ValueError("Null shard requires the exact frozen TRUE_GANG plan, not a literal 'gang' guess")
    return public_shard


def seconds_between(start, end):
    if not start or not end:
        return None
    seconds = (datetime.fromisoformat(end.replace("Z", "+00:00")) -
               datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    if seconds < 0 or not math.isfinite(seconds):
        raise ValueError("Invalid measured phase interval")
    return seconds


DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS operations (
 operation_id TEXT PRIMARY KEY, campaign TEXT, case_id TEXT, interface TEXT,
 status TEXT, input_sha256 TEXT, native_input_sha256 TEXT, raw_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
 operation_id TEXT NOT NULL REFERENCES operations, attempt_id TEXT NOT NULL,
 stage_id TEXT NOT NULL, shard_id TEXT NOT NULL, attempt_number INTEGER,
 outcome TEXT, raw_json TEXT NOT NULL, PRIMARY KEY(operation_id,attempt_id));
CREATE TABLE IF NOT EXISTS evidence (
 path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints (
 operation_id TEXT NOT NULL REFERENCES operations, shard_id TEXT, attempt_id TEXT,
 generation INTEGER, source TEXT REFERENCES evidence, raw_json TEXT NOT NULL,
 PRIMARY KEY(operation_id,shard_id,attempt_id,generation));
CREATE TABLE IF NOT EXISTS commands (
 operation_id TEXT NOT NULL REFERENCES operations, shard_id TEXT, attempt_id TEXT,
 command_id TEXT NOT NULL, segment INTEGER, phase TEXT, finished_at TEXT,
 started_at_derived TEXT, wall_seconds REAL, exit_code INTEGER,
 checkpoint_step INTEGER, ns_per_day REAL, raw_json TEXT NOT NULL,
 PRIMARY KEY(operation_id,shard_id,command_id,segment,finished_at));
CREATE TABLE IF NOT EXISTS measurements (
 operation_id TEXT NOT NULL REFERENCES operations, attempt_id TEXT NOT NULL,
 shard_id TEXT NOT NULL, replica_id TEXT NOT NULL, name TEXT NOT NULL,
 value_json TEXT, unit TEXT, quality TEXT NOT NULL, source TEXT NOT NULL,
 PRIMARY KEY(operation_id,attempt_id,shard_id,replica_id,name));
CREATE TABLE IF NOT EXISTS observations (
 source TEXT NOT NULL, line_number INTEGER NOT NULL, observed_at TEXT,
 attempt_id TEXT, pod_uid TEXT, raw_json TEXT NOT NULL,
 PRIMARY KEY(source,line_number));
CREATE INDEX IF NOT EXISTS measurement_lookup ON measurements(name,operation_id,attempt_id);
CREATE INDEX IF NOT EXISTS observation_attempt ON observations(attempt_id,observed_at);
CREATE TABLE IF NOT EXISTS lifecycle_events (
 operation_id TEXT REFERENCES operations, attempt_id TEXT, shard_id TEXT,
 sequence INTEGER, kind TEXT, phase TEXT, occurred_at TEXT, code TEXT,
 PRIMARY KEY(operation_id,sequence));
CREATE TABLE IF NOT EXISTS allocations (
 operation_id TEXT REFERENCES operations, attempt_id TEXT, pod_uid TEXT,
 node_name TEXT, scheduled_at TEXT, first_observed_at TEXT, last_observed_at TEXT,
 release_lower_bound TEXT, release_upper_bound TEXT, gpu_count INTEGER,
 resources_json TEXT, images_json TEXT, PRIMARY KEY(operation_id,attempt_id,pod_uid));
"""


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(DDL)

    def evidence(self, path):
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?)",
                        (str(path), digest.hexdigest(), path.stat().st_size))
        return str(path)

    def measure(self, op, attempt, shard, replica, name, value, unit, quality, source):
        if value is None and quality not in ("unknown", "not_applicable"):
            raise ValueError("missing measurement must not look measured")
        self.db.execute("INSERT OR REPLACE INTO measurements VALUES (?,?,?,?,?,?,?,?,?)",
            (op, attempt or "", shard, replica, name, canonical(value), unit, quality, str(source)))

    def verified_file(self, reference, candidates):
        if reference is None:
            return None
        for path in candidates:
            if not path.is_file():
                continue
            if (path.stat().st_size != reference["size_bytes"] or
                    hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]):
                raise ValueError("Retained native file differs from its source hash/size")
            self.evidence(path)
            return path
        return None

    def commands(self, op, shard, attempt, commands, seen, file_resolver, source):
        for command in commands:
            identity = (shard, command["step_id"], command["segment"], command["finished_at"])
            if identity in seen:
                continue
            seen.add(identity)
            argv = command["command"]
            phase = "simulation_and_native_initialization" if "mdrun" in argv else (
                "analysis" if any(x in argv for x in ("energy", "eneconv", "trjcat", "check")) else "input_preparation")
            wall = command["wall_seconds"]
            if type(wall) not in (int, float) or not math.isfinite(wall) or wall < 0:
                raise ValueError("Invalid measured command wall time")
            start = (datetime.fromisoformat(command["finished_at"]) - timedelta(seconds=wall)).isoformat()
            log = file_resolver(command.get("log")) if phase == "simulation_and_native_initialization" else None
            seconds = native_counter_seconds(log.read_text(errors="replace")) if log else None
            detail = {**command, "ledger_source": str(source),
                      "ledger_attempt_quality": "attributed" if attempt else "unknown",
                      "native_mdrun_counter_wall_seconds": seconds,
                      "native_mdrun_counter_source": str(log) if log else None}
            self.db.execute("INSERT OR REPLACE INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (op, shard, attempt or "", *identity[1:3], phase, identity[3], start, wall,
                 command["exit_code"], command.get("checkpoint_step"),
                 command.get("performance_ns_per_day"), canonical(detail)))

    def cohort(self, path, recovered, interface):
        for receipt_file in sorted(path.glob("*/receipt.json")):
            root = receipt_file.parent
            receipt, request = load(receipt_file), load(root / "request.json")
            status = load(root / "status.json", {})
            op = receipt.get("operation_id")
            if not op:
                continue
            prov = load(root / "provenance.json", {})
            self.db.execute("INSERT OR REPLACE INTO operations VALUES (?,?,?,?,?,?,?,?)",
                (op, path.parent.name, root.name, interface, status.get("operation", {}).get("status"),
                 receipt.get("input_sha256"), prov.get("tpr_sha256"), canonical(status)))
            for name in ("request.json", "receipt.json", "status.json", "provenance.json", "events.json", "result.json", "frozen-plan.json"):
                if (root / name).exists():
                    self.evidence(root / name)
            for event in load(root / "events.json", {"data": []})["data"]:
                self.db.execute("INSERT OR REPLACE INTO lifecycle_events VALUES (?,?,?,?,?,?,?,?)",
                    (op, event["attempt_id"], event["shard_id"], event["sequence"], event["kind"],
                     event["phase"], event["occurred_at"], event["code"]))
            attempts = {}
            modes = frozen_stage_modes(root, op, status)
            for stage in status.get("batch", {}).get("stages", []):
                for attempt in stage["attempts"]:
                    shard = native_shard(attempt["shard_id"], modes.get(stage["stage_id"]), request["parameters"]["jobs"])
                    key = (shard, attempt["attempt_number"])
                    if key in attempts:
                        raise ValueError("Ambiguous native job/attempt across stages")
                    attempts[key] = attempt["attempt_id"]
                    self.db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?,?,?,?)",
                        (op, attempt["attempt_id"], stage["stage_id"], *key,
                         attempt["outcome"], canonical(attempt)))
            bucket = recovered / root.name
            inventory = load(bucket / "storage-inventory.json", {})
            if inventory:
                self.measure(op, "", "", "", "retained_customer_objects_bytes", inventory["retained_bytes"],
                             "bytes", "measured", self.evidence(bucket / "storage-inventory.json"))
            self.measure(op, "", "", "", "input_bundle_bytes", prov.get("bundle_bytes"), "bytes",
                         "measured" if prov.get("bundle_bytes") is not None else "unknown", root / "provenance.json")
            seen = set()
            for manifest_path in sorted(bucket.glob("*/attempt-*/checkpoint-*.json")):
                manifest = load(manifest_path)
                state = manifest["state"]
                if state["operation_id"] != op:
                    raise ValueError("checkpoint operation identity mismatch")
                shard = state["job_id"]
                attempt = attempts.get((shard, int(manifest_path.parent.name.removeprefix("attempt-"))))
                if not attempt:
                    raise ValueError("checkpoint has no matching public execution attempt")
                source = self.evidence(manifest_path)
                self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?)",
                    (op, shard, attempt, state["generation"], source, canonical(manifest)))
                references = {file["path"]: file for file in manifest["files"]}

                def checkpoint_file(name):
                    ref = references.get(name)
                    return self.verified_file(ref, [bucket / "native-versions" / ref["sha256"]]) if ref else None

                # Recovered states repeat older commands. The first retained
                # execution owns the row; final artifacts only fill real gaps.
                self.commands(op, shard, attempt, state["commands"], seen, checkpoint_file, source)
            native_files = {}
            artifacts = receipt.get("verified_artifacts", [])
            artifact_by_hash = {item["sha256"]: Path(item["path"]) for item in artifacts}
            for item in artifacts:
                if item.get("semantic_type") != "gromacs-workflow-result/v1":
                    continue
                result_path = Path(item["path"])
                if not result_path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Native result escapes this retained case")
                result_path = self.verified_file(item, [result_path])
                if result_path is None:
                    continue
                result = load(result_path)
                shard = result.get("job_id")
                if result.get("operation_id") != op or shard not in {j["id"] for j in request["parameters"]["jobs"]}:
                    raise ValueError("Native result identity mismatch")
                references = {file["path"]: file for file in result["files"]}

                def result_file(name):
                    ref = references.get(name)
                    candidate = artifact_by_hash.get(ref["sha256"]) if ref else None
                    if candidate is None:
                        return None
                    if not candidate.resolve().is_relative_to(root.resolve()):
                        raise ValueError("Native file escapes this retained case")
                    return self.verified_file(ref, [candidate])

                possible = [aid for (job, _), aid in attempts.items() if job == shard]
                # Do not assign commands copied through a restart to the latest
                # attempt just because its final result retained them.
                attempt = possible[0] if len(possible) == 1 else None
                self.commands(op, shard, attempt, result["commands"], seen, result_file, result_path)
                for name in references:
                    if name.endswith(".log"):
                        candidate = result_file(name)
                        if candidate:
                            native_files[(shard, name)] = candidate
            for job in request["parameters"]["jobs"]:
                shard = job["id"]
                for step in job["steps"]:
                    if step["command"] != "mdrun":
                        continue
                    replica = step["id"]
                    args = step["args"]
                    prefix = args[args.index("-deffnm") + 1] if "-deffnm" in args else replica
                    logs = sorted((bucket / "native").glob(prefix + ".part*.log"))
                    if not logs:
                        logs = [path for (job, name), path in native_files.items()
                                if job == shard and re.fullmatch(re.escape(prefix) + r"\.part\d+\.log", name)]
                    text = "\n".join(p.read_text(errors="replace") for p in logs)
                    data = protocol(text)
                    source = ";".join(self.evidence(p) for p in logs) or "native log not available"
                    for name, unit in (("particle_count", "particles"), ("timestep_ps", "ps")):
                        self.measure(op, "", shard, replica, name, data[name], unit,
                                     "measured" if data[name] is not None else "unknown", source)
                    self.measure(op, "", shard, replica, "protocol", data, None, "measured" if logs else "unknown", source)
                    self.measure(op, "", shard, replica, "requested_steps", receipt.get("steps"), "steps",
                                 "measured" if receipt.get("steps") is not None else "unknown", receipt_file)
                    for (attempt_shard, _), attempt in attempts.items():
                        if attempt_shard != shard:
                            continue
                        commands = self.db.execute(
                            "SELECT checkpoint_step,ns_per_day,raw_json FROM commands WHERE operation_id=? "
                            "AND attempt_id=? AND shard_id=? AND command_id=? ORDER BY finished_at",
                            (op, attempt, shard, replica)).fetchall()
                        committed = [c[0] for c in commands if c[0] is not None]
                        initial = data["initial_step"]
                        # A command's cpt step is only recorded after a native
                        # dump and successful exit, then published in a manifest.
                        end = max(committed) if committed else None
                        self.measure(op, attempt, shard, replica, "durable_end_step", end, "step",
                                     "measured" if end is not None else "unknown", source)
                        resumed = any("-cpi" in json.loads(c[2])["command"] for c in commands)
                        if committed and initial is not None and not resumed:
                            self.measure(op, attempt, shard, replica, "durable_interval", [initial, end], "steps",
                                         "derived", "initial-step and committed native checkpoint; no -cpi in this attempt")
                        elif resumed:
                            self.measure(op, attempt, shard, replica, "durable_interval", None, "steps", "unknown",
                                         "resumed: correlate prior native checkpoint before assigning this attempt's interval")
                        for name in ("executed_steps", "simulation_only_seconds", "initialization_seconds",
                                     "checkpoint_seconds", "export_seconds", "gpu_allocation_at", "gpu_release_at"):
                            self.measure(op, attempt, shard, replica, name, None, None, "unknown",
                                         "requires phase/attempt correlation; raw lifecycle and samples retained")
                        rates = [c[1] for c in commands if c[1] is not None]
                        self.measure(op, attempt, shard, replica, "native_ns_per_day", rates or None, "ns/day",
                                     "measured" if rates else "unknown", source)
                        dt = data["timestep_ps"]
                        speed = [dt * 86400 / rate for rate in rates] if dt is not None and rates else None
                        self.measure(op, attempt, shard, replica, "native_ms_per_step", speed, "ms/step",
                                     "derived" if speed else "unknown", "dt_ps * 86400 / native_ns_per_day")

    def samples(self, path):
        for source in sorted(path.glob("*.jsonl")):
            with source.open() as file:
                for line_number, line in enumerate(file, 1):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        # An actively running observer may not yet have finished
                        # its last line. Preserve raw file; re-index after drain.
                        continue
                    pod = row.get("pod")
                    meta = pod.get("metadata", {}) if isinstance(pod, dict) else {}
                    labels = row.get("labels", meta.get("labels", {}))
                    self.db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?)",
                        (str(source), line_number, row.get("observed_at"), labels.get("fs2.nebius.ai/attempt-id"),
                         row.get("pod_uid", meta.get("uid")), canonical(row)))

    def correlate_allocations(self):
        """Index raw allocations without pretending deletion polling is exact."""
        pods, gone = {}, {}
        for source, observed, raw in self.db.execute(
                "SELECT source,observed_at,raw_json FROM observations ORDER BY observed_at"):
            row = json.loads(raw)
            if source.endswith("pod-disappearance.jsonl"):
                gone.setdefault(row["pod_uid"], observed)
            pod = row.get("pod")
            if not isinstance(pod, dict):
                continue
            uid = pod["metadata"]["uid"]
            item = pods.setdefault(uid, {"first": observed})
            item.update(last=observed, pod=pod, source=source)
        known_ops = {r[0] for r in self.db.execute("SELECT operation_id FROM operations")}
        for uid, value in pods.items():
            pod = value["pod"]
            labels = pod["metadata"].get("labels", {})
            op, attempt = labels.get("fs2.nebius.ai/operation-id"), labels.get("fs2.nebius.ai/attempt-id")
            if op not in known_ops:
                continue
            status, spec = pod["status"], pod["spec"]
            scheduled = next((c.get("lastTransitionTime") for c in status.get("conditions", [])
                              if c["type"] == "PodScheduled" and c["status"] == "True"), None)
            endings = [c["state"]["terminated"]["finishedAt"] for c in status.get("containerStatuses", [])
                       if "terminated" in c.get("state", {})]
            gpu_count = sum(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
                            for c in spec["containers"])
            self.db.execute("INSERT OR REPLACE INTO allocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (op, attempt, uid, spec.get("nodeName"), scheduled, value["first"], value["last"],
                 max(endings) if endings else None, gone.get(uid), gpu_count,
                 canonical({c["name"]: c.get("resources", {}) for c in spec["containers"]}),
                 canonical({c["name"]: c.get("imageID") for c in status.get("containerStatuses", [])})))
            init = []
            expected = [c["name"] for c in spec.get("initContainers", [])]
            for container in status.get("initContainerStatuses", []):
                term = container.get("state", {}).get("terminated", {})
                duration = seconds_between(term.get("startedAt"), term.get("finishedAt"))
                init.append({"name": container["name"], "seconds": duration,
                             "started_at": term.get("startedAt"), "finished_at": term.get("finishedAt"),
                             "exit_code": term.get("exitCode")})
            complete = (bool(expected) and set(expected) == {row["name"] for row in init}
                        and all(row["seconds"] is not None for row in init))
            self.measure(op, attempt, "", "pod:" + uid, "init_container_process_seconds",
                         {"containers": init, "total_seconds": sum(row["seconds"] for row in init) if complete else None,
                          "scope": "Kubernetes process lifetime; includes interpreter/download/validation/extraction, not transfer-only"},
                         "seconds", "measured" if complete else "unknown", value["source"])
            states = {c["name"]: c.get("state", {}) for c in status.get("containerStatuses", [])}
            stage_end = states.get("scientific-stage", {}).get("terminated", {}).get("finishedAt")
            collector = states.get("artifact-collector", {})
            collector_end = collector.get("terminated", {}).get("finishedAt")
            lower = upper = None
            if stage_end and collector_end:
                # A collector which finished before the worker has zero process
                # tail, not negative export time. This is not Pod release time.
                lower = upper = max(0, (datetime.fromisoformat(collector_end.replace("Z", "+00:00")) -
                                       datetime.fromisoformat(stage_end.replace("Z", "+00:00"))).total_seconds())
            elif stage_end and "running" in collector:
                lower = seconds_between(stage_end, value["last"])
                upper = seconds_between(stage_end, gone.get(uid))
            self.measure(op, attempt, "", "pod:" + uid, "post_stage_collector_tail_bounds",
                         {"lower": lower, "upper": upper, "started_at": stage_end,
                          "collector_finished_at": collector_end, "last_observed_at": value["last"],
                          "pod_disappeared_at": gone.get(uid),
                          "scope": "collector after worker exit only; overlapping checkpoint/export before exit is excluded"},
                         "seconds", "bounded" if lower is not None or upper is not None else "unknown", value["source"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--recovered", type=Path, required=True)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--interface", choices=("REST", "MCP", "agent-skill-MCP"), default="REST")
    args = parser.parse_args()
    index = Ledger(args.database)
    with index.db:
        index.cohort(args.cohort, args.recovered, args.interface)
        index.samples(args.telemetry)
        index.correlate_allocations()
    print(json.dumps({table: index.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("operations", "attempts", "checkpoints", "commands", "measurements", "observations", "allocations", "lifecycle_events")}))
    index.db.close()


if __name__ == "__main__":
    main()
