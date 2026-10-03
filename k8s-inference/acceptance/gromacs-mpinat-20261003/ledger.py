"""Rebuild a queryable SQLite evidence ledger; never infer success from timing.

The public platform's PostgreSQL is authoritative. This is an offline analytical
index of immutable evidence, not a billing database or an admission controller.
Unmeasured quantities remain null with an explicit reason/source.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
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
            for name in ("request.json", "receipt.json", "status.json", "provenance.json", "events.json", "result.json"):
                if (root / name).exists():
                    self.evidence(root / name)
            for event in load(root / "events.json", {"data": []})["data"]:
                self.db.execute("INSERT OR REPLACE INTO lifecycle_events VALUES (?,?,?,?,?,?,?,?)",
                    (op, event["attempt_id"], event["shard_id"], event["sequence"], event["kind"],
                     event["phase"], event["occurred_at"], event["code"]))
            attempts = {}
            for stage in status.get("batch", {}).get("stages", []):
                for attempt in stage["attempts"]:
                    key = (attempt["shard_id"], attempt["attempt_number"])
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
                for command in state["commands"]:
                    # Recovered states contain older commands. Index each
                    # execution only once, in the first attempt retaining it.
                    identity = (command["step_id"], command["segment"], command["finished_at"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    argv = command["command"]
                    phase = "simulation_and_native_initialization" if "mdrun" in argv else (
                        "analysis" if any(x in argv for x in ("energy", "eneconv")) else "input_preparation")
                    wall = command["wall_seconds"]
                    start = (datetime.fromisoformat(command["finished_at"]) - timedelta(seconds=wall)).isoformat()
                    self.db.execute("INSERT OR REPLACE INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (op, shard, attempt, *identity[:2], phase, identity[2], start, wall,
                         command["exit_code"], command.get("checkpoint_step"),
                         command.get("performance_ns_per_day"), canonical(command)))
            for job in request["parameters"]["jobs"]:
                shard = job["id"]
                for step in job["steps"]:
                    if step["command"] != "mdrun":
                        continue
                    replica = step["id"]
                    args = step["args"]
                    prefix = args[args.index("-deffnm") + 1] if "-deffnm" in args else replica
                    logs = sorted((bucket / "native").glob(prefix + ".part*.log"))
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
            item.update(last=observed, pod=pod)
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
