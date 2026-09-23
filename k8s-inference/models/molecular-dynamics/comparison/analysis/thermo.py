"""Native production log/energy adapters; normalized units are explicit."""
from pathlib import Path
import re

import numpy as np

from geometry import ValidationError, finite

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
AMU_A3_TO_G_CM3 = 1.66053906660


def number(value):
    return float(value.replace("D", "E").replace("d", "e"))


def native_thermo(path, kind, timestep_ps, total_mass_amu):
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
                rows.append({"step": row["TS"], "time_ps": row["TS"] * timestep_ps, "temperature_K": row["TEMP"], "pressure_bar": row["PRESSURE"], "density_g_cm3": total_mass_amu / row["VOLUME"] * AMU_A3_TO_G_CM3, "potential_kJ_mol": row["POTENTIAL"] * 4.184})
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
                columns = parts
                continue
            if columns and parts:
                if len(parts) != len(columns) or not re.fullmatch(NUMBER, parts[0]):
                    columns = None
                    continue
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
    text = Path(log_path).read_text()
    duration_ns = production_steps * timestep_ps / 1000.
    result = {"native_ns_per_day": None, "native_loop_seconds": None, "scope": "not measured/parsed; no substitution with queue or workflow wall time"}
    if engine == "gromacs":
        values = re.findall(rf"^\s*Performance:\s*({NUMBER})", text, re.M)
        if len(values) == 1:
            result.update(native_ns_per_day=number(values[0]), scope="GROMACS native Performance footer, production log only")
    elif engine == "amber":
        values = re.findall(rf"ns/day\s*=\s*({NUMBER})", text)
        if len(values) == 1:
            result.update(native_ns_per_day=number(values[0]), scope="AMBER native ns/day production footer")
    elif engine == "lammps":
        loops = re.findall(rf"Loop time of ({NUMBER}) on .*? for (\d+) steps", text)
        if loops and sum(int(n) for _, n in loops) == production_steps:
            seconds = sum(number(s) for s, _ in loops)
            result.update(native_loop_seconds=seconds, native_ns_per_day=duration_ns * 86400 / seconds, scope="sum of production LAMMPS loop times; real units")
    elif engine == "namd":
        # NAMD's benchmark is a steady-loop estimate, not total production wall.
        values = re.findall(rf"Benchmark time:.*?({NUMBER})\s+s/step", text)
        if values:
            seconds_per_step = number(values[-1])
            result.update(native_ns_per_day=timestep_ps / 1000 * 86400 / seconds_per_step, scope="last native NAMD Benchmark s/step estimate; not workflow duration", native_seconds_per_step=seconds_per_step)
    if result["native_ns_per_day"] is not None and (not np.isfinite(result["native_ns_per_day"]) or result["native_ns_per_day"] <= 0):
        raise ValidationError("invalid native performance")
    return result
