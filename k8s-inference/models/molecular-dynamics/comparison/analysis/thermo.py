"""Native production log/energy adapters; normalized units are explicit."""
from pathlib import Path
import re

import numpy as np

from geometry import ValidationError, finite

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
AMU_A3_TO_G_CM3 = 1.66053906660


def number(value):
    return float(value.replace("D", "E").replace("d", "e"))


def native_thermo(path, kind, timestep_ps, total_mass_amu, *, boundary_receipts=None):
    if isinstance(path, list):
        if kind != "lammps_log" or not path or len({str(Path(p).resolve()) for p in path}) != len(path):
            raise ValidationError("distinct ordered LAMMPS thermo segments required")
        joined = []
        for segment, source in enumerate(path):
            rows = native_thermo(source, kind, timestep_ps, total_mass_amu)
            for index, row in enumerate(rows):
                if joined and row["step"] == joined[-1]["step"] and index == 0:
                    if row["time_ps"] != joined[-1]["time_ps"]:
                        raise ValidationError("segment boundary thermo time differs")
                    if boundary_receipts is not None:
                        boundary_receipts.append({"step": row["step"], "next_segment_index": segment, "next_segment_file": str(Path(source).resolve()), "retained_closed_segment_observation": joined[-1], "omitted_next_segment_initialization_observation": row,
                                                  "action": "use actual preceding closed-step observation, not a second initialization sample; native differences retained explicitly"})
                    continue
                if joined and row["step"] <= joined[-1]["step"]:
                    raise ValidationError("thermo steps duplicated away from a segment boundary")
                joined.append(row)
        return joined
    text = Path(path).read_text()
    pressure_not_computed = kind == "amber_mdout" and "reported pressure is always 0 because it is not calculated" in " ".join(text.lower().split())
    rows = []
    if kind == "gromacs_xvg":
        legends = {int(i): name for i, name in re.findall(r'@\s+s(\d+)\s+legend\s+"([^"]+)"', text)}
        columns = {v.lower(): k + 1 for k, v in legends.items()}
        if not {"temperature", "pressure", "density"} <= set(columns):
            raise ValidationError("GROMACS XVG requires labeled Temperature, Pressure, Density")
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith(("#", "@")):
                continue
            values = [number(x) for x in line.split()]
            row = {"time_ps": values[0], "temperature_K": values[columns["temperature"]], "pressure_bar": values[columns["pressure"]], "density_g_cm3": values[columns["density"]] / 1000.}
            if "potential" in columns:
                row["potential_kJ_mol"] = values[columns["potential"]]
            rows.append(row)
    elif kind == "namd_log":
        names = None
        group_pressure = "PRESSURE CONTROL IS GROUP-BASED" in text
        for line in text.splitlines():
            if line.startswith("ETITLE:"):
                names = line.split()[1:]
            elif line.startswith("ENERGY:"):
                if not names:
                    raise ValidationError("NAMD ENERGY without ETITLE")
                values = line.split()[1:]
                if len(values) != len(names):
                    raise ValidationError("NAMD energy field count differs")
                row = dict(zip(names, map(number, values)))
                sample = {"step": row["TS"], "time_ps": row["TS"] * timestep_ps, "temperature_K": row["TEMP"], "pressure_bar": row["GPRESSURE" if group_pressure else "PRESSURE"], "atomic_pressure_bar": row["PRESSURE"], "density_g_cm3": total_mass_amu / row["VOLUME"] * AMU_A3_TO_G_CM3, "potential_kJ_mol": row["POTENTIAL"] * 4.184}
                if "GPRESSURE" in row:
                    sample["group_pressure_bar"] = row["GPRESSURE"]
                rows.append(sample)
    elif kind == "amber_mdout":
        # The averages/RMS footer repeats NSTEP; it is not another sample.
        text = re.split(r"A\s+V\s+E\s+R\s+A\s+G\s+E\s+S", text)[0]
        chunks = re.split(r"(?=\bNSTEP\s*=)", text)[1:]
        for chunk in chunks:
            fields = dict(re.findall(rf"(NSTEP|TIME\(PS\)|TEMP\(K\)|PRESS|EPtot|VOLUME|Density)\s*=\s*({NUMBER})", chunk))
            if not {"NSTEP", "TIME(PS)", "TEMP(K)", "PRESS"} <= fields.keys():
                raise ValidationError("incomplete AMBER NSTEP sample")
            row = {"step": number(fields["NSTEP"]), "time_ps": number(fields["TIME(PS)"]), "temperature_K": number(fields["TEMP(K)"]), "pressure_bar": number(fields["PRESS"])}
            if "Density" in fields:
                row["density_g_cm3"] = number(fields["Density"])
            elif "VOLUME" in fields:
                row["density_g_cm3"] = total_mass_amu / number(fields["VOLUME"]) * AMU_A3_TO_G_CM3
            if "EPtot" in fields:
                row["potential_kJ_mol"] = number(fields["EPtot"]) * 4.184
            rows.append(row)
    elif kind == "lammps_log":
        columns = None
        for line in text.splitlines():
            parts = line.split()
            if parts and parts[0] == "Step" and {"Temp", "Press"} <= set(parts):
                if len(parts) != len(set(parts)):
                    raise ValidationError("duplicate LAMMPS thermo fields")
                columns = parts
                continue
            if columns and parts:
                if line.startswith(("Loop time of ", "ERROR", "Total wall time:")):
                    columns = None
                    continue
                if not re.fullmatch(NUMBER, parts[0]):
                    # SHAKE statistics and other explicitly labeled native
                    # diagnostics can be interleaved inside the thermo table.
                    continue
                if len(parts) != len(columns):
                    raise ValidationError("incomplete LAMMPS thermo row")
                values = dict(zip(columns, map(number, parts)))
                row = {"step": values["Step"], "time_ps": values.get("Time", values["Step"] * timestep_ps), "temperature_K": values["Temp"], "pressure_bar": values["Press"] * 1.01325}
                # In units real, thermo Time is femtoseconds, not picoseconds.
                if "Time" in values:
                    row["time_ps"] /= 1000.
                if "Density" in values:
                    row["density_g_cm3"] = values["Density"]
                elif "Volume" in values:
                    row["density_g_cm3"] = total_mass_amu / values["Volume"] * AMU_A3_TO_G_CM3
                if "PotEng" in values:
                    row["potential_kJ_mol"] = values["PotEng"] * 4.184
                # Preserve every original numerical field, including both
                # setup/closed observations at a native restart boundary.
                row.update({f"native_{key}": value for key, value in values.items()})
                rows.append(row)
    else:
        raise ValidationError(f"unsupported native thermodynamic source {kind}")
    if not rows:
        raise ValidationError(f"no native thermodynamic samples in {path}")
    for row in rows:
        finite(list(row.values()), "native thermodynamics")
        if "step" in row and row["step"] != int(row["step"]):
            raise ValidationError("noninteger thermodynamic step")
        if pressure_not_computed:
            if row["pressure_bar"] != 0:
                raise ValidationError("AMBER not-computed pressure warning conflicts with nonzero PRESS")
            row["uncomputed_pressure_placeholder_bar"] = row.pop("pressure_bar")
    return rows


def production_rows(rows, origin_step, origin_time_ps, production_steps, timestep_ps):
    result = []
    duration = production_steps * timestep_ps
    for row in rows:
        value = dict(row)
        value["time_ps"] -= origin_time_ps
        if "step" in value:
            value["step"] -= origin_step
            if abs(value["time_ps"] - value["step"] * timestep_ps) > 1e-3:
                raise ValidationError("native thermo step/time origin mismatch")
        if -1e-3 <= value["time_ps"] <= duration + 1e-3:
            result.append(value)
    times = np.array([row["time_ps"] for row in result])
    if len(times) < 2 or np.any(np.diff(times) <= 0):
        raise ValidationError("missing/duplicate/out-of-order production thermo samples")
    if abs(times[-1] - duration) > 1e-3:
        raise ValidationError("thermodynamic log does not reach final production time")
    # Exclude initialization, matching trajectory comparison's (0, duration].
    return [row for row in result if row["time_ps"] > 1e-3]


def descriptive(values):
    a = finite(values, "summary values")
    if not len(a):
        raise ValidationError("no summary values")
    return {"n": len(a), "mean": float(a.mean()), "std": float(a.std(ddof=1)) if len(a) > 1 else 0., "min": float(a.min()), "max": float(a.max()), "uncertainty": "sample SD, not independent-sample SEM; correlated 1 ns trajectory"}


def native_performance(log_path, engine, production_steps, timestep_ps):
    if isinstance(log_path, list):
        if engine != "lammps" or not log_path or len({str(Path(p).resolve()) for p in log_path}) != len(log_path):
            raise ValidationError("distinct ordered production-only LAMMPS performance logs required")
        text = "\n".join(Path(path).read_text() for path in log_path)
    else:
        text = Path(log_path).read_text()
    duration_ns = production_steps * timestep_ps / 1000.
    result = {"native_ns_per_day": None, "native_loop_seconds": None, "scope": "not measured/parsed; no substitution with queue or workflow wall time"}
    if engine == "gromacs":
        values = re.findall(rf"^\s*Performance:\s*({NUMBER})", text, re.M)
        if len(values) == 1:
            result.update(native_ns_per_day=number(values[0]), scope="GROMACS native Performance footer, production log only")
    elif engine == "amber":
        full = re.findall(rf"Average timings for all steps:\s*\|\s*Elapsed\(s\)\s*=\s*({NUMBER}).*?\|\s*ns/day\s*=\s*({NUMBER})", text, re.S)
        if len(full) == 1:
            result.update(native_loop_seconds=number(full[0][0]), native_ns_per_day=number(full[0][1]), scope="AMBER native all-steps production footer, not last-window timing")
        else:
            values = re.findall(rf"ns/day\s*=\s*({NUMBER})", text)
            if len(values) == 1:
                result.update(native_ns_per_day=number(values[0]), scope="AMBER native ns/day production footer")
    elif engine == "lammps":
        loops = re.findall(rf"Loop time of ({NUMBER}) on .*? for (\d+) steps", text)
        if loops and sum(int(n) for _, n in loops) == production_steps:
            seconds = sum(number(s) for s, _ in loops)
            result.update(native_loop_seconds=seconds, native_ns_per_day=duration_ns * 86400 / seconds, scope="sum of production LAMMPS loop times; real units")
    elif engine == "namd":
        # Prefer the final accumulated native production estimate over startup
        # benchmark windows. Neither is the end-to-end worker/process duration.
        averages = re.findall(rf"^PERFORMANCE:\s*(\d+)\s+averaging\s+({NUMBER})\s+ns/day,\s*({NUMBER})\s+sec/step", text, re.M)
        if averages:
            result.update(native_ns_per_day=number(averages[-1][1]), native_seconds_per_step=number(averages[-1][2]), native_final_timing_step=int(averages[-1][0]), scope="final native NAMD accumulated PERFORMANCE estimate; not worker/process duration")
        else:
            values = re.findall(rf"Benchmark time:.*?({NUMBER})\s+s/step", text)
            if values:
                seconds_per_step = number(values[-1])
                result.update(native_ns_per_day=timestep_ps / 1000 * 86400 / seconds_per_step, scope="last native NAMD Benchmark s/step estimate; not workflow duration", native_seconds_per_step=seconds_per_step)
    if result["native_ns_per_day"] is not None and (not np.isfinite(result["native_ns_per_day"]) or result["native_ns_per_day"] <= 0):
        raise ValidationError("invalid native performance")
    return result
