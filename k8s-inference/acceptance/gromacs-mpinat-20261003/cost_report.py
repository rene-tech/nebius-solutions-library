"""Offline MPINAT evidence/cost report. No API, credentials, or cluster access.

Read the ledger in a read-only transaction. Optional saved cohort directories
add delivery assertions and identify index lag; they never overwrite ledger
progress. Prices are dated public-list scenarios, not invoices. GPU reservation
is not utilization, and native mdrun wall time is not simulation-only time.

Example (run from this directory):
  python3 cost_report.py --database /private/measurements.sqlite \
    --cohort /private/rest-b/cohort-1 --output /private/new-report.json

Cross-operation retries require an explicit --retry-map JSON list:
  [{"logical_id":"retry-group", "reason":"operator-authorized retry",
    "members":[{"operation_id":"...", "shard_id":"benchmark",
                "replica_id":"repeat-1"}, ...]}]
Intentional repeat-1/2/3 are distinct logical work, not three independent
scientific samples. Groups must preserve the original TPR and step domain.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics

from ledger import union_steps


REFERENCES = Path(__file__).with_name("public_cost_references.json")
SCHEMA = "fs2.gromacs-efficiency-cost/v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def metric(value, unit, status="derived", reason=None, **extra):
    if value is None:
        status = "unknown"
    elif isinstance(value, (int, float)) and not math.isfinite(value):
        raise ValueError("nonfinite measurement")
    return dict(value=value, unit=unit, status=status, reason=reason, **extra)


def timestamp(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return result


def interval_union(intervals):
    """Retain disjoint ranges as well as their length; steps are integers."""
    merged = []
    for start, end in sorted(intervals):
        if (type(start) is not int or type(end) is not int
                or start < 0 or end < start):
            raise ValueError("invalid native step interval")
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    assert sum(b - a for a, b in merged) == union_steps(intervals)
    return merged


def work_accounting(intervals, requested=None, executed_steps=None):
    """Durable range union is useful work; total retry/lost work needs execution.

    Missing intervals do not mean zero work. A known full requested domain is
    sufficient for exact useful work despite unknown failed attempts, but not
    for exact executed work or retry waste. Repeated checkpoint generations
    must already have been deduplicated by the ledger.
    """
    known = [x for x in intervals if x is not None]
    ranges = interval_union(known)
    if requested is not None:
        interval_union([requested])
        if any(a < requested[0] or b > requested[1] for a, b in known):
            raise ValueError("durable range outside requested domain")
    lower = union_steps(known)
    covered = requested is not None and lower == requested[1] - requested[0]
    complete = bool(intervals) and (all(x is not None for x in intervals) or covered)
    duplicate = sum(b - a for a, b in known) - lower
    if executed_steps is not None:
        if type(executed_steps) is not int or executed_steps < lower:
            raise ValueError("executed steps smaller than durable work")
    return {
        "ranges_start_exclusive_end_inclusive": ranges,
        "durable_steps": metric(lower if complete else None, "steps",
                                reason=None if complete else "missing attempt progress"),
        "known_durable_steps_lower_bound": metric(lower, "steps", "lower_bound"),
        "duplicate_durable_steps_lower_bound": metric(duplicate, "steps", "lower_bound",
            "not total retry waste; uncommitted/lost execution remains unmeasured"),
        "executed_steps": metric(executed_steps, "steps", "measured",
                                 "requires independent executed-step evidence"),
        "retry_or_lost_steps": metric(executed_steps - lower
            if executed_steps is not None and complete else None, "steps",
            reason="executed work minus durable union, excluding intentional distinct replicas"),
        "requested_domain_fully_durable": covered,
    }


def preset_shape(platform, preset, region, source=None):
    match = re.fullmatch(r"(\d+)gpu-(\d+)vcpu-(\d+)gb", preset or "")
    return {"platform": platform, "preset": preset, "region": region,
            "node_gpus": int(match[1]) if match else None,
            "node_vcpus": int(match[2]) if match else None,
            "node_ram_gib": int(match[3]) if match else None, "source": source}


def hourly_price(shape, allocated_gpus, references, on_date):
    """GPU-share allocation model, plus nonadditive full-node scenario.

    L40S VM CPU/RAM cost is apportioned by allocated GPU fraction, never Pod
    requests. Full-node hourly price is not a sum-able per-Pod invoice.
    """
    pricing = references["pricing"]
    common = {"currency": pricing["currency"], "scenario": pricing["basis"],
              "date": on_date, "source": "nebius_pricing",
              "apportionment": "allocated GPUs / VM GPUs; not CPU utilization"}
    unknown = {**common, "status": "unknown", "allocated_share_per_hour": None,
               "full_node_per_hour": None, "gpu_component_per_hour": None}
    rate = pricing["platforms"].get(shape.get("platform"))
    if (rate is None or rate.get("gpu_hour") is None or shape.get("region") != pricing["region"]
            or on_date != pricing["valid_on"]):
        return {**unknown, "reason": "unverified platform, region, or pricing date"}
    if type(allocated_gpus) is not int or allocated_gpus <= 0:
        return {**unknown, "reason": "missing positive allocation count"}
    # H100's per-GPU rate includes its fixed CPU/RAM share; a GPU-only price
    # cannot be separated from the official table.
    gpu = allocated_gpus * rate["gpu_hour"] if rate["rate_kind"] == "components" else None
    if rate["rate_kind"] == "components" and (rate.get("vcpu_hour") is None or rate.get("gib_hour") is None):
        return {**unknown, "gpu_component_per_hour": gpu, "reason": "CPU or RAM price unknown"}
    components = [shape.get(x) for x in ("node_gpus", "node_vcpus", "node_ram_gib")]
    if components not in rate["presets"] or allocated_gpus > (components[0] or 0):
        return {**unknown, "gpu_component_per_hour": gpu,
                "reason": "actual VM preset missing or not in sourced preset table"}
    ngpu, cpu, ram = components
    total = ngpu * rate["gpu_hour"]
    if rate["rate_kind"] == "components":
        total += cpu * rate["vcpu_hour"] + ram * rate["gib_hour"]
    return {**common, "status": "modelled", "reason": rate["cpu_ram_scope"],
            "allocated_share_per_hour": total * allocated_gpus / ngpu,
            "full_node_per_hour": total, "gpu_component_per_hour": gpu}


def allocation_bounds(allocation):
    """Pod lifecycle proxy only; device-plugin/billable release is unknown.

    The saved observer's termination lower bound and first disappearance upper
    bound are retained. If termination is absent, first observation proves only
    a minimum lifetime. Last-seen Pod existence is not GPU-computing evidence.
    """
    start = timestamp(allocation.get("scheduled_at"))
    low = timestamp(allocation.get("release_lower_bound"))
    if low is None:
        first = timestamp(allocation.get("first_observed_at"))
        # Observer may first see a Pending Pod before it has any allocation.
        # That timestamp supplies no post-binding lifetime lower bound.
        low = first if first and start and first >= start else None
    high = timestamp(allocation.get("release_upper_bound"))
    lower = (low - start).total_seconds() if start and low else None
    upper = (high - start).total_seconds() if start and high else None
    if ((lower is not None and lower < 0) or (upper is not None and upper < 0)
            or (lower is not None and upper is not None and lower > upper)):
        raise ValueError("inconsistent allocation timestamps")
    return {"lower": lower, "upper": upper, "unit": "seconds",
            "status": "bounded_proxy" if lower is not None and upper is not None else "incomplete",
            "exact_device_release_at": None,
            "scope": "Pod scheduled-to-release observation, not actual computing or VM billing lifetime"}


def sum_bounds(rows, field, weight=lambda row: 1):
    values = {}
    for side in ("lower", "upper"):
        terms = [row[field][side] * weight(row) for row in rows
                 if row[field][side] is not None and weight(row) is not None]
        values[side] = sum(terms) if rows and len(terms) == len(rows) else None
    return {**values, "status": "bounded_proxy" if all(x is not None for x in values.values()) else "incomplete"}


def ratio_bounds(numerator, denominator, scale=1):
    if numerator is None:
        return {"lower": None, "upper": None, "status": "unknown"}
    lo, hi = denominator["lower"], denominator["upper"]
    return {"lower": numerator * scale / hi if hi and hi > 0 else None,
            "upper": numerator * scale / lo if lo and lo > 0 else None,
            "status": "bounded_proxy" if lo and hi and lo > 0 and hi > 0 else "incomplete"}


def command_metrics(rate, dt, atoms, gpu_count, wall, steps):
    for value in (rate, dt, atoms, gpu_count, wall, steps):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("negative/nonfinite command metric")
    native_steps_per_second = rate * 1000 / dt / 86400 if rate and dt else None
    wall_gpu = wall * gpu_count if wall is not None and gpu_count else None
    return {
        "native_ns_per_day": metric(rate, "ns/day", "native_reported"),
        "native_ms_per_step": metric(dt * 86400 / rate if dt and rate else None, "ms/step"),
        "native_particle_steps_per_gpu_second": metric(
            native_steps_per_second * atoms / gpu_count
            if native_steps_per_second is not None and atoms and gpu_count else None, "particle-steps/GPU-second"),
        "mdrun_wall_seconds": metric(wall, "seconds", "measured",
                                     "includes process/native initialization and timed warmup"),
        "mdrun_wall_gpu_seconds": metric(wall_gpu, "GPU-seconds", "allocation_model"),
        "durable_particle_steps_per_mdrun_gpu_second": metric(
            steps * atoms / wall_gpu if steps is not None and atoms and wall_gpu else None,
            "particle-steps/GPU-second"),
        "useful_ns_per_mdrun_gpu_hour": metric(
            steps * dt / 1000 * 3600 / wall_gpu
            if steps is not None and dt and wall_gpu else None, "ns/GPU-hour"),
        "simulation_only_seconds": metric(None, "seconds", reason="native phase timer not measured"),
        "simulation_only_cost": metric(None, "USD", reason="native phase timer not measured"),
    }


def read_cohorts(paths):
    """Allowlist saved receipt facts; do not propagate URLs, keys, or raw JSON."""
    result = {}
    for path in paths:
        for receipt_path in sorted(Path(path).glob("*/receipt.json")):
            receipt = json.loads(receipt_path.read_text())
            op = receipt.get("operation_id")
            if not op:
                continue
            artifacts = receipt.get("verified_artifacts") or []
            public_path = receipt_path.with_name("result.json")
            public = json.loads(public_path.read_text()) if public_path.is_file() else {}
            if public and public.get("operation_id") != op:
                raise ValueError("saved result operation mismatch")
            native = None
            for artifact in artifacts:
                if artifact.get("semantic_type") != "gromacs-workflow-result/v1":
                    continue
                p = Path(artifact["path"])
                # Result references must remain in the explicit saved cohort.
                if not p.resolve().is_relative_to(receipt_path.parent.resolve()):
                    raise ValueError("native result path outside saved case directory")
                if p.stat().st_size > 16 * 1024 * 1024 or sha256(p) != artifact["sha256"]:
                    raise ValueError("saved native result digest/size mismatch")
                n = json.loads(p.read_text())
                if n["operation_id"] != op:
                    raise ValueError("native result operation mismatch")
                native = {"sha256": artifact["sha256"], "status": n.get("status"),
                          "engine_id": n.get("engine_id"), "mpi_ranks": n.get("mpi_ranks"),
                          "recipe_sha256": n.get("recipe_sha256")}
                break
            result[op] = {"case_id": receipt.get("case"), "receipt_state": receipt.get("state"),
                "receipt_path": str(receipt_path), "receipt_sha256": sha256(receipt_path),
                "verified_artifact_count_recorded_by_sdk": len(artifacts),
                "artifact_bytes_recorded_by_sdk": sum(a.get("size_bytes", 0) for a in artifacts),
                "terminal_status": public.get("terminal_status"),
                "semantic_validation_status": (public.get("semantic_validation") or {}).get("status"),
                "native_result_reverified": native,
                "scope": "saved SDK receipt assertion; this report rehashes native result only, not all artifacts"}
    return result


def node_shapes(db):
    versions = defaultdict(dict)
    for row in db.execute("SELECT source,line_number,raw_json FROM observations WHERE source LIKE '%/nodes.jsonl'"):
        for node in json.loads(row["raw_json"]).get("items", []):
            metadata = node.get("metadata", {})
            labels = metadata.get("labels", {})
            shape = preset_shape(labels.get("node.kubernetes.io/instance-type"),
                                 labels.get("nebius.com/resource-preset"),
                                 labels.get("topology.kubernetes.io/region"))
            shape["capacity_type"] = labels.get("capacity.fs2.nebius/type")
            identity = canonical(shape)
            versions[metadata["name"]][identity] = {**shape, "source": {
                "path": row["source"], "line": row["line_number"]}}
    return {name: next(iter(values.values())) if len(values) == 1 else {
        "status": "unknown", "reason": "conflicting node shapes; timestamp correlation required"}
        for name, values in versions.items()}


def measurement_index(db):
    index = {}
    for row in db.execute("SELECT * FROM measurements"):
        index[(row["operation_id"], row["attempt_id"], row["shard_id"], row["replica_id"], row["name"])] = {
            "value": json.loads(row["value_json"]) if row["value_json"] is not None else None,
            "quality": row["quality"], "source": row["source"]}
    return index


def logical_work(replica_rows, retry_map):
    lookup = {tuple(row[k] for k in ("operation_id", "shard_id", "replica_id")): row
              for row in replica_rows}
    consumed, groups = set(), []
    ids = set()
    for entry in retry_map:
        if not entry.get("reason") or not entry.get("logical_id") or len(entry.get("members", [])) < 2:
            raise ValueError("retry group needs explicit identity, reason, and at least two members")
        if entry["logical_id"] in ids:
            raise ValueError("duplicate logical id")
        ids.add(entry["logical_id"])
        keys = [tuple(m[k] for k in ("operation_id", "shard_id", "replica_id")) for m in entry["members"]]
        if len(set(keys)) != len(keys) or consumed.intersection(keys):
            raise ValueError("logical retry membership is not unique")
        rows = [lookup[k] for k in keys]
        # Missing identity is never evidence of equivalence.
        science = [canonical(r["science_identity"]) for r in rows]
        if (len(set(science)) != 1 or any(v is None for v in rows[0]["science_identity"].values())):
            raise ValueError("retry group science/step identity mismatch or missing evidence")
        # Repeat identifiers separate intended work even when their TPR matches.
        if len({r["replica_id"] for r in rows}) != 1:
            raise ValueError("intentional timing repeats cannot be collapsed as retries")
        consumed.update(keys)
        groups.append((entry["logical_id"], entry["reason"], rows))
    for key, row in lookup.items():
        if key not in consumed:
            groups.append(("/".join(key), "operation-local default; no cross-operation inference", [row]))
    result = []
    for identity, reason, rows in groups:
        intervals = [value for row in rows for value in row["attempt_intervals"]]
        executed = [r["work"]["executed_steps"]["value"] for r in rows]
        work = work_accounting(intervals, rows[0]["requested_domain"],
                               sum(executed) if all(x is not None for x in executed) else None)
        dt = rows[0]["science_identity"]["timestep_ps"]
        steps = work["durable_steps"]["value"]
        result.append({"logical_id": identity, "reason": reason,
                       "members": [{k: row[k] for k in ("operation_id", "shard_id", "replica_id")} for row in rows],
                       "science_identity": rows[0]["science_identity"], "work": work,
                       "useful_ns": metric(steps * dt / 1000 if steps is not None and dt else None, "ns")})
    return result


def logical_operation_costs(logical_rows, operations):
    """Charge each operation once across all its intentional repeats.

    Connected components arise only from explicit logical-retry membership.
    Per-replica occupancy cost is unknown because idle/export time has no
    defensible per-replica attribution. The operation group covers all repeats.
    """
    parents = {o["operation_id"]: o["operation_id"] for o in operations}

    def find(key):
        while parents[key] != key:
            key = parents[key]
        return key

    for work in logical_rows:
        members = sorted({m["operation_id"] for m in work["members"]})
        for op in members[1:]:
            parents[find(op)] = find(members[0])
    components = defaultdict(list)
    for op in operations:
        components[find(op["operation_id"])].append(op)
    results = []
    for members in components.values():
        ids = sorted(o["operation_id"] for o in members)
        works = [w for w in logical_rows if any(m["operation_id"] in ids for m in w["members"])]
        ns = [w["useful_ns"]["value"] for w in works]
        useful_ns = sum(ns) if ns and all(v is not None for v in ns) else None
        ps = [w["work"]["durable_steps"]["value"] * w["science_identity"]["particle_count"]
              for w in works if w["work"]["durable_steps"]["value"] is not None and w["science_identity"]["particle_count"] is not None]
        particle_steps = sum(ps) if works and len(ps) == len(works) else None
        occupancy = sum_bounds(members, "occupancy_gpu_seconds")
        cost = sum_bounds(members, "allocated_occupancy_cost")
        results.append({"operation_ids": ids, "logical_replica_ids": [w["logical_id"] for w in works],
            "useful_ns": metric(useful_ns, "ns"),
            "useful_particle_steps": metric(particle_steps, "particle-steps"),
            "occupancy_gpu_seconds": {**occupancy, "unit": "GPU-seconds"},
            "allocated_occupancy_cost": {**cost, "unit": "USD"},
            "useful_ns_per_allocated_gpu_hour": {**ratio_bounds(useful_ns, occupancy, 3600), "unit": "ns/GPU-hour"},
            "particle_steps_per_allocated_gpu_second": {**ratio_bounds(particle_steps, occupancy), "unit": "particle-steps/GPU-second"},
            "allocated_usd_per_useful_ns": {side: cost[side] / useful_ns if cost[side] is not None and useful_ns and useful_ns > 0 else None for side in ("lower", "upper")},
            "simulation_only_cost": metric(None, "USD", reason="native phase timer not measured"),
            "scope": "all intentional repeats; each operation's allocation counted once; no automatic cross-operation retry inference"})
    return results


def public_comparison(operation, replicas, commands, references):
    row = next((r for r in references["public_benchmark"]["case_rows"]
                if r["case_id"] == operation["case_id"]), None)
    rates = [r["metrics"]["native_ns_per_day"]["value"] for r in commands
             if r["exit_code"] == 0 and r["metrics"]["native_ns_per_day"]["value"] is not None]
    checks = []
    if row:
        for replica in replicas:
            science = replica["science_identity"]
            checks.append(science["particle_count"] == row["atoms"] and
                          science["timestep_ps"] == references["public_benchmark"]["timestep_ps"])
    return {"operation_id": operation["operation_id"], "case_id": operation["case_id"],
            "public_ns_per_day": metric(row["ns_per_day"] if row else None, "ns/day", "published",
                                        None if row else "no numerical row in selected primary PDF"),
            "observed_native_ns_per_day": rates,
            "observed_native_median": statistics.median(rates) if rates else None,
            "basic_atom_dt_match": all(checks) if checks else None,
            "matched_speedup": None,
            "comparison_status": "historical_context_not_matched" if row else "no_public_baseline",
            "differences": ["GTX 1080 / E3-1240v6 versus observed VM hardware",
                            "GROMACS 2018 / CUDA 8.0 versus exact recorded engine image",
                            "full TPR identity, force-field/output settings and tuning equivalence not established",
                            "published table warmup policy unspecified; corrected campaign has no forced counter reset"],
            "source": references["sources"]["mpinat_pdf"]["url"]}


def build_report(database, references, cohorts=(), retry_map=(), pricing_date="2026-10-03"):
    if references["pricing"]["currency"] != "USD":
        raise ValueError("this report's cost columns are explicitly USD; no inferred FX conversion")
    database = Path(database).resolve()
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    source_hasher = hashlib.sha256()
    # Hash exact selected database contents inside the snapshot; hashing the
    # live sqlite file alone could miss WAL pages or race another writer.
    for table in ("operations", "attempts", "commands", "measurements", "allocations", "observations"):
        source_hasher.update(table.encode())
        for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            source_hasher.update(canonical(list(row)).encode())
            source_hasher.update(b"\n")
    shapes = node_shapes(db)
    measurements = measurement_index(db)
    saved = read_cohorts(cohorts)
    operations = [dict(r) for r in db.execute("SELECT operation_id,campaign,case_id,interface,status,input_sha256,native_input_sha256 FROM operations ORDER BY operation_id")]
    attempts = [dict(r) for r in db.execute("SELECT operation_id,attempt_id,shard_id,attempt_number,outcome FROM attempts ORDER BY operation_id,attempt_number")]
    allocations = []
    for raw in db.execute("SELECT * FROM allocations ORDER BY operation_id,attempt_id,pod_uid"):
        row = {k: raw[k] for k in ("operation_id", "attempt_id", "pod_uid", "node_name", "gpu_count",
                                  "scheduled_at", "first_observed_at", "last_observed_at", "release_lower_bound", "release_upper_bound")}
        shape = shapes.get(row["node_name"], {"status": "unknown", "reason": "node telemetry absent"})
        price = hourly_price(shape, row["gpu_count"], references, pricing_date)
        bounds = allocation_bounds(row)
        cost = {side: bounds[side] / 3600 * price["allocated_share_per_hour"]
                if bounds[side] is not None and price["allocated_share_per_hour"] is not None else None
                for side in ("lower", "upper")}
        allocations.append({**row, "hardware": shape, "price": price, "occupancy_seconds": bounds,
                            "allocated_occupancy_cost": {**cost, "unit": "USD", "status": "modelled_bounds"},
                            "images": json.loads(raw["images_json"] or "{}")})

    def value(op, attempt, shard, replica, name):
        return measurements.get((op, attempt, shard, replica, name), {}).get("value")

    replica_rows = []
    op_lookup = {r["operation_id"]: r for r in operations}
    for key in sorted(k for k in measurements if k[-1] == "requested_steps" and k[1] == ""):
        op, _, shard, replica, _ = key
        associated = [a for a in attempts if a["operation_id"] == op and a["shard_id"] == shard]
        ranges = [value(op, a["attempt_id"], shard, replica, "durable_interval") for a in associated]
        executions = [value(op, a["attempt_id"], shard, replica, "executed_steps") for a in associated]
        protocol = value(op, "", shard, replica, "protocol") or {}
        initial, requested = protocol.get("initial_step"), value(op, "", shard, replica, "requested_steps")
        domain = [initial, initial + requested] if initial is not None and requested is not None else None
        executed = sum(executions) if executions and all(x is not None for x in executions) else None
        work = work_accounting(ranges, domain, executed)
        science = {"native_input_sha256": op_lookup[op]["native_input_sha256"],
                   "particle_count": value(op, "", shard, replica, "particle_count"),
                   "timestep_ps": value(op, "", shard, replica, "timestep_ps"),
                   "initial_step": initial, "requested_steps": requested}
        replica_rows.append({"operation_id": op, "shard_id": shard, "replica_id": replica,
                             "science_identity": science, "requested_domain": domain,
                             "attempt_intervals": ranges, "work": work})
    replica_lookup = {(r["operation_id"], r["shard_id"], r["replica_id"]): r for r in replica_rows}
    command_rows = []
    for raw in db.execute("SELECT * FROM commands WHERE phase='simulation_and_native_initialization' ORDER BY operation_id,finished_at"):
        row = dict(raw)
        detail = json.loads(row.pop("raw_json"))
        argv = detail["command"]
        a = [x for x in allocations if x["operation_id"] == row["operation_id"] and x["attempt_id"] == row["attempt_id"]]
        # Multiple Pods may be sequential retries/subreplacements rather than
        # a concurrent MPI shape. Require coverage of the whole command interval.
        start, end = timestamp(row["started_at_derived"]), timestamp(row["finished_at"])
        covering = [x for x in a if timestamp(x["scheduled_at"]) and timestamp(x["release_upper_bound"])
                    and timestamp(x["scheduled_at"]) <= start and timestamp(x["release_upper_bound"]) >= end]
        ngpu = sum(x["gpu_count"] for x in covering) if covering and all(x["gpu_count"] > 0 for x in covering) else None
        replica = replica_lookup.get((row["operation_id"], row["shard_id"], row["command_id"]), {})
        science = replica.get("science_identity", {})
        # Per-command work is not inferred from terminal checkpoint alone after
        # resume. Only a unique, non-resumed successful command has a start here.
        siblings = db.execute("SELECT COUNT(*) FROM commands WHERE operation_id=? AND attempt_id=? AND shard_id=? AND command_id=?",
                              (row["operation_id"], row["attempt_id"], row["shard_id"], row["command_id"])).fetchone()[0]
        steps = (row["checkpoint_step"] - science["initial_step"]
                 if siblings == 1 and "-cpi" not in argv and row["exit_code"] == 0
                 and row["checkpoint_step"] is not None and science.get("initial_step") is not None else None)
        flags = {flag: argv[argv.index(flag) + 1] for flag in
                 ("-ntmpi", "-ntomp", "-nb", "-pme", "-bonded", "-update", "-npme", "-nsteps", "-resetstep")
                 if flag in argv and argv.index(flag) + 1 < len(argv)}
        metrics = command_metrics(row["ns_per_day"], science.get("timestep_ps"),
                                  science.get("particle_count"), ngpu, row["wall_seconds"], steps)
        prices = [x["price"]["allocated_share_per_hour"] for x in covering]
        metrics["mdrun_wall_cost"] = metric(row["wall_seconds"] / 3600 * sum(prices)
            if prices and all(p is not None for p in prices) else None, "USD", "modelled",
            "command wall includes native initialization; allocation share, not invoice")
        command_rows.append({**row, "command_sha256": hashlib.sha256(canonical(argv).encode()).hexdigest(),
            "native_flags": flags, "timing_policy": "forced_counter_reset" if "-resethway" in argv or "-resetstep" in argv
            else "no_forced_reset_warmup_inclusive", "gpu_allocation_count": ngpu,
            "allocation_count_quality": "observed Pod envelope, not proof of computing",
            "pod_uids": [x["pod_uid"] for x in covering], "metrics": metrics})
    db.close()

    for attempt in attempts:
        op, aid = attempt["operation_id"], attempt["attempt_id"]
        a = [x for x in allocations if x["operation_id"] == op and x["attempt_id"] == aid]
        c = [x for x in command_rows if x["operation_id"] == op and x["attempt_id"] == aid]
        work = [{"replica_id": r["replica_id"], "work": work_accounting(
            [value(op, aid, r["shard_id"], r["replica_id"], "durable_interval")], r["requested_domain"],
            value(op, aid, r["shard_id"], r["replica_id"], "executed_steps"))}
            for r in replica_rows if r["operation_id"] == op and r["shard_id"] == attempt["shard_id"]]
        attempt.update(allocation_count=len(a), native_command_count=len(c),
                       replica_work=work,
                       occupancy_gpu_seconds={**sum_bounds(a, "occupancy_seconds", lambda x: x["gpu_count"]), "unit": "GPU-seconds"},
                       allocated_occupancy_cost={**sum_bounds(a, "allocated_occupancy_cost"), "unit": "USD"},
                       simulation_only_cost=metric(None, "USD", reason="native phase timer not measured"))
    comparisons = []
    for operation in operations:
        op = operation["operation_id"]
        a = [x for x in allocations if x["operation_id"] == op]
        attempt_count = sum(x["operation_id"] == op for x in attempts)
        covered_attempts = {x["attempt_id"] for x in a}
        r = [x for x in replica_rows if x["operation_id"] == op]
        c = [x for x in command_rows if x["operation_id"] == op]
        useful = [x["work"]["durable_steps"]["value"] * x["science_identity"]["timestep_ps"] / 1000
                  for x in r if x["work"]["durable_steps"]["value"] is not None and x["science_identity"]["timestep_ps"] is not None]
        ns = sum(useful) if r and len(useful) == len(r) else None
        ps = [x["work"]["durable_steps"]["value"] * x["science_identity"]["particle_count"]
              for x in r if x["work"]["durable_steps"]["value"] is not None and x["science_identity"]["particle_count"] is not None]
        particle_steps = sum(ps) if r and len(ps) == len(r) else None
        occupancy = sum_bounds(a, "occupancy_seconds", lambda x: x["gpu_count"])
        cost = sum_bounds(a, "allocated_occupancy_cost")
        if not attempt_count or len(covered_attempts) != attempt_count:
            occupancy = cost = {"lower": None, "upper": None, "status": "incomplete_attempt_coverage"}
        operation.update(saved_delivery=saved.get(op), attempt_count=attempt_count,
                         observed_allocation_attempt_count=len(covered_attempts),
                         useful_ns=metric(ns, "ns"),
                         useful_particle_steps=metric(particle_steps, "particle-steps"),
                         occupancy_gpu_seconds={**occupancy, "unit": "GPU-seconds"},
                         useful_ns_per_allocated_gpu_hour={**ratio_bounds(ns, occupancy, 3600), "unit": "ns/GPU-hour"},
                         particle_steps_per_allocated_gpu_second={**ratio_bounds(particle_steps, occupancy), "unit": "particle-steps/GPU-second"},
                         allocated_occupancy_cost={**cost, "unit": "USD"},
                         simulation_only_cost=metric(None, "USD", reason="native phase timer not measured"))
        comparisons.append(public_comparison(operation, r, c, references))
    examples = []
    for count in (1, 2, 4, 8, 16):
        per_node = 1 if count == 1 else min(8, count)
        preset = "1gpu-16vcpu-200gb" if count == 1 else "8gpu-128vcpu-1600gb"
        price = hourly_price(preset_shape("gpu-h100-sxm", preset, "eu-north1"), per_node, references, pricing_date)
        nodes = max(1, count // 8)
        examples.append({"gpus": count, "nodes": nodes, "preset": preset,
                         "allocated_share_usd_hour": price["allocated_share_per_hour"] * nodes if price["allocated_share_per_hour"] is not None else None,
                         "whole_nodes_usd_hour": price["full_node_per_hour"] * nodes if price["full_node_per_hour"] is not None else None,
                         "status": "pricing_example_not_capacity_or_performance_evidence"})
    logical = logical_work(replica_rows, retry_map)
    return {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": {"database": str(database), "snapshot_rows_sha256": source_hasher.hexdigest(),
                       "ledger_open_mode": "read-only transaction", "report_source_sha256": sha256(__file__),
                       "reference_canonical_sha256": hashlib.sha256(canonical(references).encode()).hexdigest()},
            "coverage": {"operation_count": len(operations), "attempt_count": len(attempts),
                         "allocation_count": len(allocations), "native_command_count": len(command_rows),
                         "saved_receipt_count": len(saved),
                         "saved_operations_missing_from_ledger": sorted(set(saved) - set(op_lookup)),
                         "campaign_status_counts": dict(Counter(f"{o['campaign']}:{o['status']}" for o in operations))},
            "limitations": ["No GPU utilization or invoice derived from allocation. VM lifetime outside Pod scope excluded.",
                            "Pod lifetime release bounds are not exact device-plugin release times.",
                            "Missing attempt telemetry prevents a complete operation cost; observed rows are retained.",
                            "Native simulation-only, initialization, checkpoint/export phase costs and uncommitted retry work remain unknown.",
                            "Native rates are warmup-policy-specific; do not pool corrected and forced-reset runs.",
                            "Ledger progress is durable evidence, not proof of scientific equilibrium or independent replicas.",
                            "Cross-operation retries are separate unless explicitly mapped; a case-name match is insufficient."],
            "price_reference": references["pricing"], "public_sources": references["sources"],
            "shape_examples": examples, "operations": operations, "attempts": attempts,
            "allocations": allocations, "replicas": replica_rows, "commands": command_rows,
            "logical_work": logical, "logical_operation_costs": logical_operation_costs(logical, operations),
            "public_comparisons": comparisons}


def write_summary_csv(report, path):
    """Small, allowlisted per-operation table; blank is unknown, never zero."""
    fields = ["campaign", "case_id", "operation_id", "interface", "status",
              "saved_sdk_receipt_state", "semantic_validation_status", "gpu_platforms",
              "native_rate_sample_count", "timing_policies", "native_median_ns_day",
              "public_gtx1080_ns_day", "comparison_status", "useful_ns",
              "occupancy_gpu_seconds_lower", "occupancy_gpu_seconds_upper",
              "allocation_cost_usd_lower", "allocation_cost_usd_upper", "allocation_cost_status",
              "useful_ns_per_gpu_hour_lower", "useful_ns_per_gpu_hour_upper",
              "simulation_only_cost_usd", "simulation_only_cost_status"]
    comparisons = {c["operation_id"]: c for c in report["public_comparisons"]}
    with Path(path).open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for op in sorted(report["operations"], key=lambda o: (o["campaign"], o["case_id"], o["operation_id"])):
            comparison = comparisons[op["operation_id"]]
            commands = [c for c in report["commands"] if c["operation_id"] == op["operation_id"]]
            allocations = [a for a in report["allocations"] if a["operation_id"] == op["operation_id"]]
            saved = op.get("saved_delivery") or {}
            row = {k: op[k] for k in ("campaign", "case_id", "operation_id", "interface", "status")}
            row.update(saved_sdk_receipt_state=saved.get("receipt_state"),
                       semantic_validation_status=saved.get("semantic_validation_status"),
                       gpu_platforms=";".join(sorted({a["hardware"].get("platform") or "unknown" for a in allocations})) or "unknown",
                       native_rate_sample_count=len(comparison["observed_native_ns_per_day"]),
                       timing_policies=";".join(sorted({c["timing_policy"] for c in commands})) or "unknown",
                       native_median_ns_day=comparison["observed_native_median"],
                       public_gtx1080_ns_day=comparison["public_ns_per_day"]["value"],
                       comparison_status=comparison["comparison_status"], useful_ns=op["useful_ns"]["value"],
                       allocation_cost_status=op["allocated_occupancy_cost"]["status"],
                       simulation_only_cost_usd=None, simulation_only_cost_status="unknown")
            for side in ("lower", "upper"):
                row[f"occupancy_gpu_seconds_{side}"] = op["occupancy_gpu_seconds"][side]
                row[f"allocation_cost_usd_{side}"] = op["allocated_occupancy_cost"][side]
                row[f"useful_ns_per_gpu_hour_{side}"] = op["useful_ns_per_allocated_gpu_hour"][side]
            # Do not let case labels turn a spreadsheet cell into a formula.
            row = {k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in row.items()}
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, action="append", default=[])
    parser.add_argument("--references", type=Path, default=REFERENCES)
    parser.add_argument("--retry-map", type=Path)
    parser.add_argument("--pricing-date", default="2026-10-03")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path, help="optional per-operation summary; creates a new file only")
    args = parser.parse_args()
    refs = json.loads(args.references.read_text())
    retry_map = json.loads(args.retry_map.read_text()) if args.retry_map else []
    report = build_report(args.database, refs, args.cohort, retry_map, args.pricing_date)
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    if args.csv_output:
        write_summary_csv(report, args.csv_output)
    print(json.dumps({"report": str(args.output), "sha256": sha256(args.output), "coverage": report["coverage"]}))


if __name__ == "__main__":
    main()
