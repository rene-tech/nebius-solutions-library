"""Read every DCD frame and report finite observables and native throughput.

NVE energy drift and NPT observables are evidence, not a convergence assertion.
The validator preserves failed repetitions and does not choose the best run.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import struct

from fs2_namd.worker import binary_vectors, xsc_step


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
                    rows.append((values[0], values[10], values[11], values[17]))
    if not rows:
        raise ValueError("no energy observables")
    baseline = rows[0][1]
    return {"first_total_kcal_per_mol": baseline, "last_total_kcal_per_mol": rows[-1][1],
            "max_relative_total_energy_deviation": max(abs(r[1] - baseline) for r in rows) / max(abs(baseline), 1),
            "mean_temperature_K": statistics.fmean(r[2] for r in rows),
            "minimum_temperature_K": min(r[2] for r in rows), "maximum_temperature_K": max(r[2] for r in rows),
            "minimum_volume_A3": min(r[3] for r in rows), "maximum_volume_A3": max(r[3] for r in rows)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repetitions = []
    for path in sorted(args.campaign.glob("rep-*/result.json")):
        result = json.loads(path.read_text())
        row = {"job": result["job_id"], "status": result["status"], "error": result["error"]}
        if result["status"] == "succeeded":
            data = path.parent / "data"
            commands = [c for c in result["commands"] if c["step_id"] == "production"]
            row["native_ns_per_day"] = statistics.median(c["performance_ns_per_day"] for c in commands)
            row["native_wall_seconds"] = sum(c["wall_seconds"] for c in commands)
            row["cpu_user_seconds"] = sum(c["cpu_user_seconds"] for c in commands)
            row["first_energy_log_observed_seconds"] = [c["first_energy_log_observed_seconds"] for c in commands]
            row["scientific_observables"] = energy_drift([data / c["log"] for c in commands])
            row["trajectories"] = {str(p.relative_to(data)): dcd(p) for p in sorted(data.rglob("production.part*.dcd"))}
            if not row["trajectories"]:
                raise ValueError("qualification requires readable production trajectories")
            for p in data.rglob("production.coor"):
                row["atoms"] = binary_vectors(p)
                if binary_vectors(p.with_suffix(".vel")) != row["atoms"]:
                    raise ValueError("final checkpoint vectors disagree")
                row["final_step"] = xsc_step(p.with_suffix(".xsc"))
        telemetry = args.campaign / (result["job_id"] + "-gpu.csv")
        if telemetry.exists():
            with telemetry.open() as stream:
                rows = list(csv.DictReader(stream, skipinitialspace=True))
            for key, title in (("utilization.gpu [%]", "gpu_utilization_percent"), ("power.draw [W]", "power_watts"), ("memory.used [MiB]", "gpu_memory_MiB")):
                values = [float(r[key].split()[0]) for r in rows if key in r and r[key].split()[0].replace(".", "", 1).isdigit()]
                if values:
                    row[title] = {"mean": statistics.fmean(values), "max": max(values), "samples": len(values)}
        repetitions.append(row)
    speeds = [r["native_ns_per_day"] for r in repetitions if "native_ns_per_day" in r]
    report = {"repetitions": repetitions, "total_repetitions": len(repetitions),
              "successful_repetitions": len(speeds), "single_trajectory": True, "MPS": False,
              "median_ns_per_day": statistics.median(speeds) if speeds else None,
              "min_ns_per_day": min(speeds) if speeds else None, "max_ns_per_day": max(speeds) if speeds else None}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
