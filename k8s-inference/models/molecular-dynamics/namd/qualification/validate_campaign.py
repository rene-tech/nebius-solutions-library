"""Read every DCD frame and report finite observables and native throughput.

NVE energy drift and NPT observables are evidence, not a convergence assertion.
The validator preserves failed repetitions and does not choose the best run.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import re
import statistics
import struct

from fs2_namd.worker import binary_vectors, colvars_step, xsc_step
from fs2_namd.colvars_state import blocks


def dcd(path):
    with path.open("rb") as stream:
        first = stream.read(4)
        endian = next((e for e in ("<", ">") if struct.unpack(e + "i", first)[0] == 84), None)
        if endian is None:
            raise ValueError("unsupported DCD header")
        stream.seek(0)
        def record():
            value = stream.read(4)
            if not value:
                return None
            size = struct.unpack(endian + "i", value)[0]
            if not 0 <= size <= 64 * 1024**2:
                raise ValueError("DCD record exceeds bounded parser")
            data = stream.read(size)
            suffix = stream.read(4)
            if len(data) != size or len(suffix) != 4 or struct.unpack(endian + "i", suffix)[0] != size:
                raise ValueError("truncated DCD record")
            return data
        header = record()
        if header[:4] != b"CORD":
            raise ValueError("not a coordinate trajectory")
        control = struct.unpack(endian + "20i", header[4:])
        frames, start, interval, fixed, cell = control[0], control[1], control[2], control[8], control[10]
        if fixed:
            raise ValueError("fixed-atom DCD requires a separate reader qualification")
        record()  # title
        atoms = struct.unpack(endian + "i", record())[0]
        observed = 0
        while True:
            data = record()
            if data is None:
                break
            if cell:
                if len(data) != 48 or not all(math.isfinite(v[0]) for v in struct.iter_unpack(endian + "d", data)):
                    raise ValueError("non-finite or unsupported DCD cell record")
                data = record()
            for axis in range(3):
                if axis:
                    data = record()
                if data is None or len(data) != atoms * 4 or not all(math.isfinite(v[0]) for v in struct.iter_unpack(endian + "f", data)):
                    raise ValueError("truncated/non-finite DCD coordinates")
            observed += 1
        if observed != frames or frames < 1:
            raise ValueError("DCD frame header/count mismatch or no frames")
        return {"atoms": atoms, "frames": frames, "first_step": start, "interval_steps": interval,
                "last_step": start + (frames - 1) * interval}


def energy_drift(paths):
    rows = []
    for path in paths:
        with path.open() as source:
            for line in source:
                if line.startswith("ENERGY:"):
                    values = list(map(float, line.split()[1:]))
                    if not all(math.isfinite(value) for value in values):
                        raise ValueError("non-finite scientific observable")
                    rows.append((values[0], values[10], values[11], values[17]))
    if not rows:
        raise ValueError("no energy observables")
    baseline = rows[0][1]
    return {"first_total_kcal_per_mol": baseline, "last_total_kcal_per_mol": rows[-1][1],
            "max_relative_total_energy_deviation": max(abs(r[1] - baseline) for r in rows) / max(abs(baseline), 1),
            "mean_temperature_K": statistics.fmean(r[2] for r in rows),
            "minimum_temperature_K": min(r[2] for r in rows), "maximum_temperature_K": max(r[2] for r in rows),
            "minimum_volume_A3": min(r[3] for r in rows), "maximum_volume_A3": max(r[3] for r in rows)}


def production_timing(commands):
    simulated_ns = sum((command["checkpoint_step"] - command["configured_first_step"]) * command["timestep_fs"] / 1_000_000 for command in commands)
    wall = sum(command["wall_seconds"] for command in commands)
    if simulated_ns <= 0 or wall <= 0:
        raise ValueError("production timing lacks a positive simulated duration and measured wall time")
    return {"production_simulated_ns": simulated_ns,
            "production_process_ns_per_day": simulated_ns / wall * 86400,
            "cpu_user_cores_during_native_production": sum(command["cpu_user_seconds"] for command in commands) / wall}


def radius_metadynamics(data):
    """Validate this fixture's explicit hill list survives a native restart.

    This parser qualifies only the supplied explicit-hill radius-metadynamics
    fixtures (ungridded or grid + keepHills), not arbitrary Colvars algorithms
    or free-energy convergence.
    """
    states = sorted(path for path in data.rglob("production.part*.colvars.state")
                    if re.fullmatch(r"production\.part\d{6}\.colvars\.state", path.name))
    if not states:
        return None
    previous, summaries, previous_text, grid_round_trips, pmfs = [], [], None, [], []
    for path in states:
        text = path.read_text()
        if re.search(r"(?m)^\s*hills_energy(?:_gradients)?[ \t]*(?:\{|$)", text):
            for name in ("hills_energy", "hills_energy_gradients"):
                if not grid_blocks(text, name):
                    raise ValueError("native Colvars state lacks a metadynamics grid")
            if previous_text is not None:
                loaded_path = path.with_name(path.name.removesuffix(".colvars.state") + ".loaded.colvars.state")
                grid_round_trips.append(verify_grid_round_trip(previous_text, loaded_path.read_text()))
            pmf = path.with_name(path.name.removesuffix(".colvars.state") + ".pmf")
            values = [list(map(float, line.split())) for line in pmf.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
            if not values or not all(len(row) == 2 and all(math.isfinite(v) for v in row) for row in values):
                raise ValueError("missing or non-finite radius-metadynamics PMF grid")
            pmfs.append({"path": str(pmf.relative_to(data)), "points": len(values),
                         "min_coordinate_A": min(row[0] for row in values), "max_coordinate_A": max(row[0] for row in values),
                         "min_value_kcal_per_mol": min(row[1] for row in values), "max_value_kcal_per_mol": max(row[1] for row in values),
                         "free_energy_convergence_claimed": False})
        hills = []
        for body in re.findall(r"\bhill\s*\{([^}]*)\}", text, re.DOTALL):
            values = dict(line.split(maxsplit=1) for line in body.strip().splitlines())
            values = {key: float(value) for key, value in values.items()}
            if set(values) != {"step", "weight", "centers", "widths"} or not all(math.isfinite(v) for v in values.values()):
                raise ValueError("unexpected/non-finite radius-metadynamics hill state")
            hills.append(values)
        if not hills or hills[:len(previous)] != previous:
            raise ValueError("native Colvars restart lost or changed prior metadynamics hills")
        steps = [h["step"] for h in hills]
        if any(b <= a for a, b in zip(steps, steps[1:])) or steps[-1] != colvars_step(path):
            raise ValueError("fixture hill sequence and native bias timestep disagree")
        previous = hills
        previous_text = text
        summaries.append({"path": str(path.relative_to(data)), "step": colvars_step(path),
                          "hills": len(hills), "first_hill_step": steps[0], "last_hill_step": steps[-1]})
    trajectories = []
    for path in sorted(data.rglob("production.part*.colvars.traj")):
        rows = [list(map(float, line.split())) for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not rows or not all(math.isfinite(v) for row in rows for v in row):
            raise ValueError("empty/non-finite Colvars trajectory")
        trajectories.append({"path": str(path.relative_to(data)), "records": len(rows),
                             "first_step": rows[0][0], "last_step": rows[-1][0]})
    if len(trajectories) != len(states):
        raise ValueError("missing Colvars trajectory segment")
    return {"bias_history_prefix_preserved": True, "states": summaries, "trajectories": trajectories,
            "grid_round_trips": grid_round_trips, "pmfs": pmfs}


def grid_blocks(text, name):
    """Read the actual unbraced one-dimensional native qualification grid.

    The braced form remains readable for explicit older parser fixtures. Native
    output is a keyword, grid_parameters block, then exactly sizes[0] numbers.
    This intentionally does not claim multidimensional/vector-Colvar coverage.
    """
    found = list(blocks(text, name))
    for marker in re.finditer(r"(?m)^[ \t]*" + re.escape(name) + r"[ \t]*$", text):
        tail = text[marker.end():]
        if not re.match(r"\s*grid_parameters\s*\{", tail):
            raise ValueError("native metadynamics grid lacks parameters")
        parameters = blocks(tail, "grid_parameters")[0]
        metadata = {}
        for line in parameters[2].splitlines():
            if not line.strip():
                continue
            key, value = line.split(maxsplit=1)
            if key in metadata:
                raise ValueError("duplicate native metadynamics grid parameter")
            metadata[key] = value.split()
        if set(metadata) != {"n_colvars", "lower_boundaries", "upper_boundaries", "widths", "sizes"} or any(len(v) != 1 for v in metadata.values()):
            raise ValueError("unsupported native qualification grid parameters")
        fields = {k: float(v[0]) for k, v in metadata.items()}
        if not all(math.isfinite(v) for v in fields.values()):
            raise ValueError("non-finite native qualification grid parameters")
        count = int(fields["sizes"])
        if (fields["n_colvars"] != 1 or fields["sizes"] != count or count < 1 or fields["widths"] <= 0
                or not math.isclose(fields["upper_boundaries"] - fields["lower_boundaries"], count * fields["widths"])):
            raise ValueError("invalid or unsupported native qualification grid shape")
        values = []
        for token in tail[parameters[1] + 1:].split():
            try:
                value = float(token)
            except ValueError:
                break
            if not math.isfinite(value):
                raise ValueError("non-finite native metadynamics grid values")
            values.append(token)
        if len(values) != count:
            raise ValueError("native metadynamics grid value count differs from its shape")
        found.append((marker.start(), marker.end(), "grid_parameters { " + parameters[2] + " } " + " ".join(values)))
    return found


def verify_grid_round_trip(original, loaded):
    """Compare every serialized grid field/value for the explicit grid fixture."""
    summaries = {}
    for name in ("hills_energy", "hills_energy_gradients"):
        before, after = grid_blocks(original, name), grid_blocks(loaded, name)
        if not before or len(before) != len(after):
            raise ValueError("native Colvars restart lost a metadynamics grid")
        count = 0
        for (_, _, left), (_, _, right) in zip(before, after):
            a, b = left.split(), right.split()
            if len(a) != len(b):
                raise ValueError("native Colvars restart changed grid shape")
            for x, y in zip(a, b):
                try:
                    x, y = float(x), float(y)
                except ValueError:
                    if x != y:
                        raise ValueError("native Colvars restart changed grid metadata")
                else:
                    if not math.isfinite(x) or not math.isfinite(y) or not math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-14):
                        raise ValueError("native Colvars restart changed grid values")
                    count += 1
        summaries[name] = {"blocks": len(before), "numeric_values_verified": count}
    return summaries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    campaign_path = args.campaign / "campaign.json"
    campaign = {row["job"]: row for row in json.loads(campaign_path.read_text())} if campaign_path.exists() else {}
    repetitions = []
    for path in sorted(args.campaign.glob("rep-*/result.json")):
        result = json.loads(path.read_text())
        row = {"job": result["job_id"], "status": result["status"], "native_status": result["status"], "error": result["error"]}
        if result["status"] == "succeeded":
            try:
                data = path.parent / "data"
                commands = [c for c in result["commands"] if c["step_id"] == "production"]
                row["native_ns_per_day"] = statistics.median(c["performance_ns_per_day"] for c in commands)
                row["native_wall_seconds"] = sum(c["wall_seconds"] for c in commands)
                row["cpu_user_seconds"] = sum(c["cpu_user_seconds"] for c in commands)
                row.update(production_timing(commands))
                row["first_energy_log_observed_seconds"] = [c["first_energy_log_observed_seconds"] for c in commands]
                row["scientific_observables"] = energy_drift([data / c["log"] for c in commands])
                row["radius_metadynamics"] = radius_metadynamics(data)
                row["artifact_bytes"] = sum(item["size_bytes"] for item in result["files"])
                row["native_process_wall_seconds_all_stages"] = sum(c["wall_seconds"] for c in result["commands"])
                if result["job_id"] in campaign:
                    elapsed = campaign[result["job_id"]]["wall_seconds"]
                    row["local_workflow_wall_seconds"] = elapsed
                    row["local_workflow_production_ns_per_day"] = row["production_simulated_ns"] / elapsed * 86400
                    row["local_non_native_overhead_seconds"] = elapsed - row["native_process_wall_seconds_all_stages"]
                row["trajectories"] = {str(p.relative_to(data)): dcd(p) for p in sorted(data.rglob("production.part*.dcd"))}
                if not row["trajectories"]:
                    raise ValueError("qualification requires readable production trajectories")
                for p in data.rglob("production.coor"):
                    row["atoms"] = binary_vectors(p)
                    if binary_vectors(p.with_suffix(".vel")) != row["atoms"]:
                        raise ValueError("final checkpoint vectors disagree")
                    row["final_step"] = xsc_step(p.with_suffix(".xsc"))
            except (ValueError, OSError, KeyError) as exc:
                row["status"], row["error"] = "failed", str(exc)
        telemetry = args.campaign / (result["job_id"] + "-gpu.csv")
        if telemetry.exists():
            with telemetry.open() as stream:
                rows = list(csv.DictReader(stream, skipinitialspace=True))
            for key, title in (("utilization.gpu [%]", "gpu_utilization_percent"), ("power.draw [W]", "power_watts"), ("memory.used [MiB]", "gpu_memory_MiB")):
                values = [float(r[key].split()[0]) for r in rows if key in r and r[key].split()[0].replace(".", "", 1).isdigit()]
                if values:
                    row[title] = {"mean": statistics.fmean(values), "max": max(values), "samples": len(values)}
        repetitions.append(row)
    speeds = [r["native_ns_per_day"] for r in repetitions if r["status"] == "succeeded" and "native_ns_per_day" in r]
    report = {"repetitions": repetitions, "total_repetitions": len(repetitions),
              "successful_repetitions": len(speeds), "single_trajectory": True, "MPS": False,
              "median_ns_per_day": statistics.median(speeds) if speeds else None,
              "min_ns_per_day": min(speeds) if speeds else None, "max_ns_per_day": max(speeds) if speeds else None}
    report["timing_scope"] = {
        "native_ns_per_day": "Per-process median of the last ten native TIMING wall seconds/step, then median across production segments",
        "production_process_ns_per_day": "Actual production nanoseconds divided by summed production process wall time, including process startup/shutdown",
        "local_workflow_production_ns_per_day": "Production nanoseconds divided by local workflow wall time including input extraction, preparation/equilibration and local checkpoint bookkeeping",
        "excluded_from_local_workflow": "Cloud API queue, Pod scheduling/pull, Object Storage transfer and initial local input-bundle copy",
        "gpu_telemetry": "One-second samples over the whole local workflow, not production-only sampling"}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if not repetitions or len(speeds) != len(repetitions):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
