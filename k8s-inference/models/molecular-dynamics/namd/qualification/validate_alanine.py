"""Validate canonical native alanine outputs without claiming engine equivalence."""
import argparse
import json
import math
from pathlib import Path
import re
import statistics
import struct

from audit_outputs import audit
from fs2_gromacs.files import digest_file
from fs2_namd.contracts import normalize
from fs2_namd.worker import binary_vectors, log_metrics, xsc_step
from inspect_warnings import warning_lines
from make_alanine_fixture import CANONICAL_PME_GRID, CANONICAL_PME_ORDER, parm7
from validate_campaign import dcd, production_timing


ENERGY_FIELDS = "TS BOND ANGLE DIHED IMPRP ELECT VDW BOUNDARY MISC KINETIC TOTAL TEMP POTENTIAL TOTALAVG TEMPAVG PRESSURE GPRESSURE VOLUME PRESSAVG GPRESSAVG".split()


def pme_settings(path, expected_tolerance):
    """Bind acceptance to actual native PME dispatch, not only input spelling."""
    text = path.read_text()
    grids = re.findall(r"(?m)^Info:\s+PME GRID DIMENSIONS\s+(\d+)\s+(\d+)\s+(\d+)\s*$", text)
    orders = re.findall(r"(?m)^Info:\s+PME INTERPOLATION ORDER\s+(\d+)\s*$", text)
    tolerances = re.findall(r"(?m)^Info:\s+PME TOLERANCE\s+(\S+)\s*$", text)
    if len(grids) != 1 or tuple(map(int, grids[0])) != CANONICAL_PME_GRID:
        raise ValueError("native PME grid is not the canonical explicit 64x64x64 grid")
    if orders != [str(CANONICAL_PME_ORDER)]:
        raise ValueError("native PME interpolation order is not canonical order 4")
    if len(tolerances) != 1 or not math.isclose(float(tolerances[0]), expected_tolerance, rel_tol=1e-12):
        raise ValueError("native PME tolerance differs from the canonical stage")
    return {"grid": list(CANONICAL_PME_GRID), "interpolation_order": CANONICAL_PME_ORDER,
            "tolerance": float(tolerances[0]), "source": "native Info log records"}


def energy_rows(path):
    fields, result = ENERGY_FIELDS, []
    for line in path.read_text().splitlines():
        if line.startswith("ETITLE:"):
            fields = line.split()[1:]
        if line.startswith("ENERGY:"):
            values = list(map(float, line.split()[1:]))
            if len(fields) != len(values) or not all(math.isfinite(v) for v in values):
                raise ValueError("unrecognized or non-finite native energies")
            result.append(dict(zip(fields, values)))
    if not result:
        raise ValueError("no native energy output")
    return result


def vectors(path):
    count = binary_vectors(path)
    raw = path.read_bytes()
    endian = next(e for e in ("<", ">") if struct.unpack(e + "i", raw[:4])[0] == count)
    return count, struct.unpack(endian + str(count * 3) + "d", raw[4:])


def rst7_coordinates(path):
    lines = path.read_text().splitlines()
    count = int(lines[1].split()[0])
    coordinate_lines = lines[2:2 + math.ceil(count * 3 / 6)]
    values = [float(line[i:i + 12]) for line in coordinate_lines for i in range(0, len(line), 12) if line[i:i + 12].strip()]
    if len(values) != count * 3 or not all(math.isfinite(v) for v in values):
        raise ValueError("invalid canonical AMBER coordinate records")
    return count, values


def coordinate_identity(source, native):
    atoms, expected = rst7_coordinates(source)
    observed_atoms, observed = vectors(native)
    if atoms != observed_atoms:
        raise ValueError("single-point atom count differs from the canonical master")
    deviation = max(abs(a - b) for a, b in zip(expected, observed))
    if deviation > 1e-6:
        raise ValueError("single-point native output moved the canonical coordinates")
    return {"atoms": atoms, "maximum_coordinate_difference_A": deviation, "coordinate_tolerance_A": 1e-6}


def submitted_request(fixture, request_path=None):
    original = json.loads((fixture / "request.json").read_text())
    if request_path is None:
        return original
    request = json.loads(request_path.read_text())
    transport_fields = {"output_destination", "output_prefix"}
    original_physics = {k: v for k, v in normalize(original).items() if k not in transport_fields}
    submitted_physics = {k: v for k, v in normalize(request).items() if k not in transport_fields}
    if original_physics != submitted_physics:
        raise ValueError("submitted canonical request changes more than output transport")
    return request


def validate(fixture, campaign, request_path=None):
    request = submitted_request(fixture, request_path)
    provenance = json.loads((fixture / "provenance.json").read_text())
    singlepoint = provenance["ensemble"] == "singlepoint"
    results = []
    for job in request["jobs"]:
        work = campaign / job["id"]
        result_path = work / "result.json"
        result = json.loads(result_path.read_text())
        data = work / "data"
        inventory = audit(request, result, data, fixture / "input.tar.gz")
        native = data / "alanine"
        protocol = json.loads((native / "protocol.json").read_text())
        fields = parm7(native / "system.prmtop")
        mass = sum(fields["MASS"])
        report = {"job_id": job["id"], "status": "passed", "result_sha256": digest_file(result_path),
                  "input_audit": inventory, "native_logs": [], "stages": []}
        results.append(report)
        for step in job["steps"]:
            commands = [c for c in result["commands"] if c["step_id"] == step["id"]]
            if len(commands) != 1:
                raise ValueError("canonical protocol expects one native process per stage")
            command = commands[0]
            log = data / command["log"]
            pme = pme_settings(log, protocol["single_point_electrostatic_tolerance"] if singlepoint else protocol["electrostatic_target_tolerance"])
            rows = energy_rows(log)
            metrics = log_metrics(log)
            if metrics["atoms"] != len(fields["MASS"]) or metrics["timestep_fs"] != protocol["timestep_fs"]:
                raise ValueError("native atom count or timestep differs from the canonical protocol")
            report["native_logs"].append({"path": str(log), "sha256": digest_file(log), "warnings": warning_lines(log.read_text())})
            stage = {"id": step["id"], "pme": pme, "native_first_energy_step": rows[0]["TS"], "native_last_energy_step": rows[-1]["TS"],
                     "last_energy_kcal_mol_bar_A3_K": rows[-1], "native_wall_seconds": command["wall_seconds"],
                     "random_seed": metrics["random_seed"], "native_ns_per_day": metrics["performance_ns_per_day"]}
            report["stages"].append(stage)
            prefix = native / step["id"]
            if singlepoint:
                if any(row["TS"] != 0 or row["KINETIC"] != 0 for row in rows) or xsc_step(prefix.with_suffix(".xsc")) != 0:
                    raise ValueError("single-point job performed dynamics or acquired kinetic energy")
                stage["canonical_coordinate_identity"] = coordinate_identity(native / "system.rst7", prefix.with_suffix(".coor"))
                velocity_maximum = max(map(abs, vectors(prefix.with_suffix(".vel"))[1]))
                # Native temperature=0 emitted ~1e-13 round-off velocities in
                # the pinned run0 path; retain their magnitude, not an exact-zero
                # claim. Native kinetic energy and unchanged coordinates are
                # checked independently above.
                stage["max_abs_native_velocity"] = velocity_maximum
                stage["native_zero_velocity_absolute_tolerance"] = 1e-10
                if velocity_maximum > 1e-10:
                    raise ValueError("zero-temperature single point wrote material velocities")
                continue
            if step["id"] == "minimize":
                stage["actual_minimization_final_step"] = xsc_step(prefix.with_suffix(".xsc"))
                continue
            count = protocol[{"nvt": "nvt_steps", "npt": "npt_steps", "production": "production_steps"}[step["id"]]]
            first = protocol["minimization_max_iterations"]
            if step["id"] in ("npt", "production"):
                first += protocol["nvt_steps"]
            if step["id"] == "production":
                first += protocol["npt_steps"]
            end, interval = first + count, protocol["output_every_steps"]
            if metrics["configured_first_step"] != first or metrics["random_seed"] != protocol[step["id"] + "_seed"]:
                raise ValueError("native stage origin or RNG seed differs from the canonical protocol")
            path = native / ("nvt.dcd" if step["id"] == "nvt" else step["id"] + ".part000001.dcd")
            trajectory = dcd(path)
            expected = {"atoms": len(fields["MASS"]), "frames": count // interval,
                        "first_step": first + interval, "interval_steps": interval, "last_step": end}
            if trajectory != expected or xsc_step(prefix.with_suffix(".xsc")) != end or rows[-1]["TS"] != end:
                raise ValueError("canonical stage duration/frame cadence differs from the requested protocol")
            selected = [row for row in rows if first < row["TS"] <= end]
            if [row["TS"] for row in selected] != list(range(first + interval, end + 1, interval)):
                raise ValueError("canonical stage energy cadence is incomplete or duplicated")
            if any(row["VOLUME"] <= 0 or row["TEMP"] <= 0 for row in selected):
                raise ValueError("nonphysical native production temperature or volume")
            stage.update(trajectory=trajectory, duration_ps=count * protocol["timestep_fs"] / 1000,
                         mean_temperature_K=statistics.fmean(row["TEMP"] for row in selected),
                         mean_atomic_pressure_bar=statistics.fmean(row["PRESSURE"] for row in selected),
                         mean_group_pressure_bar=statistics.fmean(row["GPRESSURE"] for row in selected),
                         pressure_control_uses="native group pressure", mean_density_g_mL=statistics.fmean(mass * 1.66053906660 / row["VOLUME"] for row in selected),
                         samples=len(selected), sample_interval_ps=interval * protocol["timestep_fs"] / 1000)
            if step["id"] == "production":
                stage.update(production_timing(commands))
                stage["production_relative_frame_time_ps"] = [protocol["timestep_fs"] * interval / 1000, count * protocol["timestep_fs"] / 1000]
        report["atoms"] = len(fields["MASS"])
        report["total_charge_e"] = sum(fields["CHARGE"]) / 18.2223
    report = {"status": "passed", "mode": "single-point" if singlepoint else "dynamics", "results": results,
              "input_sha256": digest_file(fixture / "input.tar.gz"), "request_sha256": digest_file(request_path or fixture / "request.json"),
              "scientific_convergence_claimed": False, "cross_engine_equivalence_proven": False,
              "gpu_snapshot_qualified": False, "customer_ready": False}
    if singlepoint:
        points = {r["job_id"]: r["stages"][0]["last_energy_kcal_mol_bar_A3_K"] for r in results}
        report["tail_on_minus_off"] = {key: points["singlepoint-tail"][key] - points["singlepoint-no-tail"][key] for key in ENERGY_FIELDS if key != "TS"}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--request", type=Path, help="Actual hosted parameters; only output transport may differ from the immutable fixture")
    args = parser.parse_args()
    report = validate(args.fixture, args.campaign, args.request)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "mode": report["mode"], "sha256": digest_file(args.output), "output": str(args.output)}))
