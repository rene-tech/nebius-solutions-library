"""Read native completion and closed restart metadata without rewriting science."""

from __future__ import annotations

import math
import re
from pathlib import Path

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"


def native_completion(path: Path, step: dict) -> dict:
    """Require the echoed native protocol and a real final dynamics/minimum record."""
    values, nsteps, times, energies = {}, [], [], []
    timings = finished = final_minimum = time_limit = False
    summaries = False
    with path.open(errors="replace") as handle:
        for line in handle:
            for key in ("imin", "nstlim", "NATOM"):
                if match := re.search(rf"\b{key}\s*=\s*(\d+)", line, re.I):
                    values[key.lower()] = int(match.group(1))
            if match := re.search(rf"\bdt\s*=\s*({NUMBER})", line, re.I):
                values["dt_ps"] = float(match.group(1).replace("D", "E").replace("d", "e"))
            time_limit |= "Wall clock limit reached" in line
            timings |= bool(re.search(r"5\.\s+TIMINGS", line))
            finished |= bool(re.search(r"\bRun\s+done at", line))
            final_minimum |= "FINAL RESULTS" in line
            summaries |= "A V E R A G E S" in line or "R M S  F L U C T U A T I O N S" in line
            if summaries:
                continue
            if match := re.search(rf"NSTEP\s*=\s*(\d+)\s+TIME\(PS\)\s*=\s*({NUMBER})", line):
                nsteps.append(int(match.group(1)))
                times.append(float(match.group(2).replace("D", "E").replace("d", "e")))
            if match := re.search(rf"\bEtot\s*=\s*({NUMBER})", line):
                energies.append(float(match.group(1).replace("D", "E").replace("d", "e")))
            if re.search(r"(?:^|[\s=])(?:[+-]?(?:nan|inf(?:inity)?)|\*{3,})(?:\s|$)", line, re.I):
                raise ValueError("native output contains non-finite or overflowed numerical fields")
    if time_limit:
        raise InterruptedError("native timlim stopped before the declared stage completed")
    if not timings or not finished or not values.get("natom"):
        raise ValueError("PMEMD output lacks native completion/timing/atom metadata")
    if step["task"] == "dynamics":
        target = step["expected_nsteps"]
        if values.get("imin") != 0 or values.get("nstlim") != target or not nsteps or nsteps[-1] != target or max(nsteps) != target:
            raise ValueError("PMEMD native dynamics did not complete the exact expected_nsteps")
        if not energies or not times or not math.isfinite(times[-1]) or values.get("dt_ps", 0) <= 0:
            raise ValueError("PMEMD dynamics lacks finite energy/time or a positive timestep")
    elif values.get("imin") != 1 or not final_minimum:
        raise ValueError("PMEMD did not complete the declared minimization")
    return {**values, "completed_native_steps": nsteps[-1] if nsteps else None,
            "final_time_ps": times[-1] if times else None, "energy_samples": len(energies),
            "task": step["task"], "native_completion": True}


def restart_metadata(path: Path, *, require_velocities: bool) -> dict:
    """Validate native ASCII/NetCDF restart structure, finite values and atom count."""
    with path.open("rb") as handle:
        signature = handle.read(8)
    if signature.startswith(b"CDF") or signature == b"\x89HDF\r\n\x1a\n":
        import numpy as np
        from netCDF4 import Dataset

        with Dataset(path, "r") as data:
            if "atom" not in data.dimensions or "coordinates" not in data.variables:
                raise ValueError("native NetCDF restart has no atom/coordinate dimensions")
            atoms = len(data.dimensions["atom"])
            coordinates = data.variables["coordinates"]
            if coordinates.shape != (atoms, 3) or atoms < 1:
                raise ValueError("native restart coordinates have the wrong shape")
            for name in ("coordinates", "velocities"):
                if name not in data.variables:
                    continue
                array = data.variables[name]
                if array.shape != (atoms, 3):
                    raise ValueError("native restart velocity/coordinate shape disagrees")
                for start in range(0, atoms, 4096):
                    block = array[start:start + 4096]
                    if np.ma.getmaskarray(block).any() or not np.isfinite(block).all():
                        raise ValueError("native restart contains missing or non-finite values")
            has_velocities = "velocities" in data.variables
            time_ps = float(data.variables["time"][...]) if "time" in data.variables else None
            box = {}
            for name in ("cell_lengths", "cell_angles"):
                if name in data.variables:
                    value = np.asarray(data.variables[name][...])
                    if value.shape != (3,) or not np.isfinite(value).all() or (value <= 0).any():
                        raise ValueError("native restart box is invalid")
                    box[name] = value.tolist()
            if bool("cell_lengths" in box) != bool("cell_angles" in box):
                raise ValueError("native restart box lengths/angles must both be present")
        fmt = "amber-netcdf-restart"
    else:
        with path.open() as handle:
            handle.readline()
            header = handle.readline().split()
            if not header or not re.fullmatch(r"\d+", header[0]):
                raise ValueError("native ASCII restart has no atom count")
            atoms = int(header[0])
            time_ps = float(header[1].replace("D", "E")) if len(header) > 1 else None
            count = 0
            tail = []
            for line in handle:
                line = line.rstrip("\r\n")
                for start in range(0, len(line), 12):
                    field = line[start:start + 12].strip()
                    if not field:
                        continue
                    number = float(field.replace("D", "E"))
                    if not math.isfinite(number):
                        raise ValueError("native ASCII restart contains non-finite values")
                    count += 1
                    tail.append(number)
                    tail = tail[-6:]
        if atoms < 3 or count not in {atoms * 3, atoms * 3 + 6, atoms * 6, atoms * 6 + 6}:
            raise ValueError("native ASCII restart size disagrees with atom/velocity count")
        has_velocities = count >= atoms * 6
        box = {"cell_lengths": tail[:3], "cell_angles": tail[3:]} if count in {atoms * 3 + 6, atoms * 6 + 6} else {}
        if box and any(value <= 0 for value in tail):
            raise ValueError("native ASCII restart box is invalid")
        fmt = "amber-ascii-restart"
    if time_ps is not None and not math.isfinite(time_ps):
        raise ValueError("native restart time is non-finite")
    if require_velocities and (not has_velocities or time_ps is None):
        raise ValueError("dynamics restart must retain native time and velocities")
    return {"format": fmt, "atoms": atoms, "time_ps": time_ps, "velocities": has_velocities, **box}


def validate_pmemd(cwd: Path, step: dict) -> dict:
    prefix = step["output_prefix"]
    completion = native_completion(cwd / (prefix + ".mdout"), step)
    restart = restart_metadata(cwd / (prefix + ".rst7"), require_velocities=step["task"] == "dynamics")
    if restart["atoms"] != completion["natom"]:
        raise ValueError("native restart and PMEMD output atom counts disagree")
    if completion["final_time_ps"] is not None and abs(restart["time_ps"] - completion["final_time_ps"]) > 0.0011:
        raise ValueError("native restart and final PMEMD time disagree")
    return {"completion": completion, "restart": restart,
            "scientific_convergence_claimed": False, "exact_stochastic_restart_claimed": False}


def validate_tool_log(path: Path, kind: str) -> dict:
    errors, leap_summary = [], None
    with path.open(errors="replace") as handle:
        for line in handle:
            if re.search(r"(?:^|\s)(?:FATAL|Error:|Traceback \(most recent call last\))", line):
                errors.append(line[:500].strip())
            if match := re.search(r"Exiting LEaP:\s+Errors\s*=\s*(\d+)", line, re.I):
                leap_summary = int(match.group(1))
    if errors or (kind == "tleap" and leap_summary != 0):
        raise ValueError(f"native {kind} reported errors or lacked a successful LEaP summary")
    return {"kind": kind, "native_error_scan": "passed", "scientific_convergence_claimed": False}
