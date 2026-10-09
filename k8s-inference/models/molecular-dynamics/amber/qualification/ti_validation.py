"""Finite native TI derivatives and complete per-state MBAR energy output."""

import math
import re
import statistics


def validate_ti(mdin, mdout, expected_steps):
    def control(name):
        match = re.search(rf"\b{name}\s*=\s*([^,\s/]+)", mdin, re.I)
        if match is None:
            raise ValueError(f"native TI input lacks{name}")
        return float(match.group(1))
    interval, states = int(control("bar_intervall")), int(control("mbar_states"))
    match = re.search(r"(?m)^\s*mbar_lambda\s*=\s*([^\n]+)", mdin)
    if match is None:
        raise ValueError("native TI input lacks explicit MBAR lambda values")
    expected_lambdas = [float(value.strip()) for value in match.group(1).split(",") if value.strip()]
    if control("ifmbar") != 1 or states != len(expected_lambdas) or not math.isclose(control("clambda"), 0.3):
        raise ValueError("fixture must preserve explicit lambda0.30 and all declared native MBAR states")
    thermo_interval = int(control("ntpr"))
    printed_interval = math.lcm(interval, thermo_interval)
    if interval <= 0 or thermo_interval <= 0 or expected_steps % printed_interval:
        raise ValueError("MBAR cadence does not divide the frozen native stage")
    blocks, current, derivatives = [], None, []
    for line in mdout.splitlines():
        if "MBAR Energy analysis:" in line:
            current = []
            blocks.append(current)
        elif match := re.search(r"^\s*Energy at\s+(\S+)\s*=\s*(\S+)", line):
            if current is None:
                raise ValueError("MBAR energy appears outside an analysis block")
            current.append((float(match.group(1)), float(match.group(2))))
        if match := re.search(r"\bDV/DL\s*=\s*(\S+)", line):
            derivatives.append(float(match.group(1)))
    # PMEMD computes MBAR at bar_intervall but ti_print_mbar_ene is called
    # inside the native thermodynamic-output branch. Only their common output
    # steps are present in mdout; do not invent missing printed records.
    if len(blocks) != expected_steps // printed_interval:
        raise ValueError("native MBAR output has an incomplete number of scheduled energy blocks")
    values = []
    for block in blocks:
        if len(block) != states or any(not math.isclose(item[0], expected, abs_tol=1e-8) for item, expected in zip(block, expected_lambdas)):
            raise ValueError("native MBAR block lacks the exact requested lambda-state coverage")
        values.extend(energy for _, energy in block)
    if not derivatives or not all(math.isfinite(value) for value in values + derivatives):
        raise ValueError("native TI derivative or MBAR energy is missing/nonfinite")
    return {"lambda": 0.3, "mbar_states": expected_lambdas, "mbar_calculation_interval_steps": interval, "mbar_print_interval_steps": printed_interval, "mbar_energy_blocks": len(blocks), "mbar_energy_values": len(values), "mbar_energy_min_kcal_mol": min(values), "mbar_energy_max_kcal_mol": max(values), "dvdl_samples_including_native_summaries": len(derivatives), "dvdl_mean_kcal_mol": statistics.mean(derivatives), "free_energy_convergence_claimed": False}
