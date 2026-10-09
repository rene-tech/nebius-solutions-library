"""Read-only matched-image MEM numerical/timing screen, not ensemble equivalence."""
import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess

from pme_control_report import native_parameters
from qualify_candidate import save, sha, validate


def energy_blocks(text, include_statistics=False):
    """Parse GROMACS's fixed-width printed native decomposition, without rounding again."""
    result = []
    lines = text.splitlines()
    step, time_ps, statistics_frames = None, None, None
    for i, line in enumerate(lines):
        match = re.search(r"Statistics over (\d+) steps using (\d+) frames", line)
        if match:
            if not include_statistics:
                break
            step, time_ps, statistics_frames = None, None, int(match[2])
        if line.split() == ["Step", "Time"]:
            fields = lines[i + 1].split()
            step, time_ps = int(fields[0]), float(fields[1])
        if line.strip() != "Energies (kJ/mol)":
            continue
        values, tokens = {}, {}
        j = i + 1
        while j + 1 < len(lines) and lines[j].strip():
            names = [lines[j][k:k + 15].strip() for k in range(0, len(lines[j]), 15)]
            raw = lines[j + 1].split()
            row = [float(x) for x in raw]
            if len(names) != len(row) or not all(math.isfinite(x) for x in row):
                raise ValueError("invalid native energy decomposition")
            values.update(zip(names, row))
            tokens.update(zip(names, raw))
            j += 2
        if (step is None and statistics_frames is None) or not {"Potential", "Temperature", "Constr. rmsd"} <= values.keys():
            raise ValueError("native step or required energy fields missing")
        result.append({"step": step, "time_ps": time_ps, "values": values,
                       "printed_tokens": tokens, "statistics_frames": statistics_frames})
    if [r["step"] for r in result if r["statistics_frames"] is None] != [0, 10000]:
        raise ValueError("expected exact initial and final native records")
    return result


def checkpoint_dump(image, checkpoint, output, *, expected_step=10000):
    """Native CPU readback, with full stream hash and bounded retained text."""
    command = ["docker", "run", "--rm", "--network", "none", "--cpus", "1", "--memory", "512m",
               "--entrypoint", "timeout", "-v", str(checkpoint.parent.resolve()) + ":/data:ro",
               image, "30", "/opt/gromacs-mpi/bin/gmx_mpi", "dump", "-cp", "/data/" + checkpoint.name]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    digest = hashlib.sha256()
    step, nonfinite, count, prefix = None, 0, 0, []
    for raw in process.stdout:
        digest.update(raw)
        count += 1
        text = raw.decode(errors="replace")
        if count <= 120:
            prefix.append(text)
        match = re.match(r"^\s*step\s*=\s*(\d+)\s*$", text)
        if match:
            step = int(match[1])
        nonfinite += bool(re.search(r"(?<![A-Za-z])(?:nan|[+-]?inf)(?![A-Za-z])", text, re.I))
    code = process.wait(timeout=60)
    output.write_text("".join(prefix))
    return {"command": command, "checkpoint_sha256": sha(checkpoint), "exit_code": code, "step": step, "expected_step": expected_step,
            "full_dump_lines": count, "full_dump_sha256": digest.hexdigest(), "nonfinite_lines": nonfinite,
            "retained_header_sha256": sha(output), "status": "passed" if code == 0 and step == expected_step and nonfinite == 0 else "failed"}


def observation(directory, fixture, ranks, output):
    receipt = json.loads((directory / "receipt.json").read_text())
    request = json.loads((fixture / "request.json").read_text())
    workspace = directory / "workspace"
    result = json.loads((workspace / "result.json").read_text())
    checked = validate(workspace, request, ranks, True, False, receipt["worker_exit_code"])
    errors = list(checked["errors"])
    commands = [c for c in result["commands"] if "mdrun" in c["command"]]
    records = []
    for i, command in enumerate(commands, 1):
        text = (workspace / "data" / f"repeat{i}.part0001.log").read_text()
        physics = native_parameters(text, "gpu")
        errors += physics["errors"]
        expected = {"-pme": "gpu", "-pmefft": "gpu", "-bonded": "cpu", "-update": "cpu",
                    "-npme": "0" if ranks == 1 else "1"}
        argv = command["command"]
        for flag, value in expected.items():
            if argv.count(flag) != 1 or argv[argv.index(flag) + 1] != value:
                errors.append(f"repeat {i}: {flag} mismatch")
        if "-notunepme" not in argv or "-resethway" in argv:
            errors.append("timing/PME tuning flags differ")
        blocks = energy_blocks(text)
        if "LINCS WARNING" in text or any(r["values"]["Constr. rmsd"] > 1e-3 for r in blocks):
            errors.append("native constraint stability screen failed")
        if any(r["values"]["Temperature"] <= 0 or r["values"].get("Kinetic En.", 0) <= 0 for r in blocks):
            errors.append("nonpositive native kinetic state")
        gro = workspace / "data" / f"repeat{i}.gro"
        velocities = [[float(line[k:k + 8]) for k in (44, 52, 60)] for line in gro.read_text().splitlines()[2:-1]]
        if not all(math.isfinite(v) for row in velocities for v in row):
            errors.append("nonfinite final velocities")
        dump = checkpoint_dump(receipt["image"], workspace / "data" / f"fs2-repeat-{i}.cpt", output / f"checkpoint-{i}.header.txt")
        if dump["status"] != "passed":
            errors.append("independent native checkpoint read failed")
        records.append({"repeat": i, "command": argv, "native_ns_per_day": command["performance_ns_per_day"],
                        "energy_blocks": blocks, "native_averages": energy_blocks(text, True)[2:], "checkpoint": dump, "physics": physics,
                        "rank_bindings": command["rank_bindings"], "final_gro_sha256": sha(gro),
                        "finite_velocity_atoms": len(velocities),
                        "kernel_lines": [line.strip() for line in text.splitlines() if "nonbonded short-range kernels" in line],
                        "direct_gpu_lines": [line.strip() for line in text.splitlines() if "direct GPU communication" in line or "GPU-aware MPI" in line]})
    rates = [r["native_ns_per_day"] for r in records]
    return {"status": "passed" if not errors else "failed", "errors": errors, "image": receipt["image"],
            "source_revision": receipt["source_revision"], "records": records, "validation": checked,
            "rates": {"repeats": rates, "mean": statistics.mean(rates), "sample_sd": statistics.stdev(rates)},
            "original_tpr_sha256": sha(workspace / "data/original.tpr"), "finite_tpr_sha256": sha(workspace / "data/benchmark.tpr"),
            "input_sha256": receipt["input_sha256"], "request_sha256": receipt["request_sha256"],
            "native_receipt_sha256": sha(directory / "receipt.json"), "cleanup": receipt["cleanup"],
            "gpu_and_driver": (directory / "gpu.txt").read_text()}


def compare(a, b):
    errors = []
    parity = {field: a[field] == b[field] for field in ("input_sha256", "request_sha256", "original_tpr_sha256", "finite_tpr_sha256")}
    for field, good in parity.items():
        if not good:
            errors.append(field + " differs")
    pairs = []
    for x, y in zip(a["records"], b["records"]):
        if x["command"] != y["command"] or x["kernel_lines"] != y["kernel_lines"]:
            errors.append("native command/kernel selection differs")
        initial_a, initial_b = x["energy_blocks"][0]["values"], y["energy_blocks"][0]["values"]
        if initial_a != initial_b:
            errors.append("initial full energy decomposition differs at printed precision; inspect before accepting")
        final_a, final_b = x["energy_blocks"][-1]["values"], y["energy_blocks"][-1]["values"]
        pairs.append({"repeat": x["repeat"], "initial_decomposition_equal_at_printed_precision": initial_a == initial_b,
                      "final_coordinate_file_identical": x["final_gro_sha256"] == y["final_gro_sha256"],
                      "final_potential_delta_kj_mol": final_b["Potential"] - final_a["Potential"],
                      "final_temperature_delta_kelvin": final_b["Temperature"] - final_a["Temperature"]})
    if len(pairs) != 3 or any(c["status"] != "passed" for c in (a, b)):
        errors.append("three passing native repeats per image required")
    endpoint = {}
    for name, value in (("baseline", a), ("candidate", b)):
        endpoint[name] = {term: {"values": [r["energy_blocks"][-1]["values"][term] for r in value["records"]],
                                "mean": statistics.mean(r["energy_blocks"][-1]["values"][term] for r in value["records"]),
                                "sample_sd": statistics.stdev(r["energy_blocks"][-1]["values"][term] for r in value["records"])}
                          for term in ("Potential", "Temperature", "Constr. rmsd")}
    return {"status": "passed" if not errors else "failed", "errors": errors, "input_parity": parity,
            "pairs": pairs, "final_endpoint_spread": endpoint,
            "candidate_over_baseline_mean_rate": b["rates"]["mean"] / a["rates"]["mean"],
            "limits": ["Three 10k-step timing repeats from the same state; warm-up inclusive, not independent ensemble samples.",
                       "Original cadence retains initial/final records, one final XVG sample, and native averaged decomposition (not a retained energy time series).",
                       "Initial decomposition equality uses native printed precision, not full internal floating-point equality.",
                       "Final trajectories may diverge chaotically even across unchanged baseline repetitions.",
                       "Constraint residual <1e-3 is a gross-stability screen, not a force-field validation tolerance.",
                       "No confidence interval, ensemble equivalence, strong-scaling, RDMA or customer-readiness claim."]}


def printed_range_screen(baseline_blocks, candidate_blocks):
    """Descriptive screen; its reference range is explicitly the three baselines.

    A printed unit comes from the least significant digit in the native token,
    including its exponent. It is not a relative tolerance, uncertainty bound,
    or bound on unprinted/internal values. Decimal prevents binary round-off
    from deciding an exact inclusive boundary.
    """
    if len(baseline_blocks) != 3 or len(candidate_blocks) != 3:
        raise ValueError("exactly three initial decompositions per image required")
    names = set(baseline_blocks[0]["printed_tokens"])
    if any(set(block["printed_tokens"]) != names or block["step"] != 0
           for block in [*baseline_blocks, *candidate_blocks]):
        raise ValueError("initial decomposition fields/steps differ")
    terms = {}
    for term in sorted(names):
        a = [Decimal(block["printed_tokens"][term]) for block in baseline_blocks]
        b = [Decimal(block["printed_tokens"][term]) for block in candidate_blocks]
        if not all(value.is_finite() for value in a + b):
            raise ValueError("nonfinite printed decomposition")
        units = [Decimal(1).scaleb(value.as_tuple().exponent) for value in a]
        margin = max(units)
        lower, upper = min(a) - margin, max(a) + margin
        inside = [lower <= value <= upper for value in b]
        terms[term] = {"unit": {"Temperature": "K", "Pressure (bar)": "bar",
                                "Constr. rmsd": "dimensionless"}.get(term, "kJ/mol"),
                       "baseline_printed_tokens": [block["printed_tokens"][term] for block in baseline_blocks],
                       "candidate_printed_tokens": [block["printed_tokens"][term] for block in candidate_blocks],
                       "baseline_values": [float(value) for value in a],
                       "candidate_values": [float(value) for value in b],
                       "baseline_min": float(min(a)), "baseline_max": float(max(a)),
                       "baseline_printed_units": [float(value) for value in units],
                       "one_printed_unit_margin": float(margin),
                       "inclusive_lower": float(lower), "inclusive_upper": float(upper),
                       "candidate_within_bounds": inside, "all_within_bounds": all(inside),
                       "zero_baseline_values": sum(value == 0 for value in a)}
    return {"screen_status": "within_bounds" if all(t["all_within_bounds"] for t in terms.values()) else "outside_bounds",
            "terms": terms,
            "rule": "Each term: baseline minimum minus one coarsest baseline printed unit through baseline maximum plus that unit, inclusive.",
            "scientific_equivalence_established": False,
            "selection": "Supplement requested after strict printed-equality failure; not a pre-registered scientific acceptance criterion.",
            "limits": ["Only initial common-state printed values; no envelope is imposed on diverging final trajectories.",
                       "Three baseline repeats are an observed range, not a confidence interval or population bound.",
                       "One printed unit is a display resolution, not a numerical accuracy or force-field error tolerance.",
                       "No relative tolerance or zero-value division; every native term and individual repeat is retained.",
                       "This supplement does not alter the status of the strict comparison or native artifacts."]}


def supplement(args):
    original = json.loads(args.strict_summary.read_text())
    if original["ranks"] != args.ranks or not all(original["input_parity"].values()):
        raise ValueError("supplement requires the original matched-input/rank comparison")
    initial, sources = {}, {}
    request = json.loads((args.fixture / "request.json").read_text())
    for name in ("baseline", "candidate"):
        directory, saved = getattr(args, name), original[name]
        if (sha(args.fixture / "input.tar.gz") != saved["input_sha256"]
                or sha(args.fixture / "request.json") != saved["request_sha256"]):
            raise ValueError("fixture differs from strict comparison")
        if sha(directory / "receipt.json") != saved["native_receipt_sha256"]:
            raise ValueError("native receipt differs from strict comparison")
        receipt = json.loads((directory / "receipt.json").read_text())
        checked = validate(directory / "workspace", request, args.ranks, True, False, receipt["worker_exit_code"])
        if checked["status"] != "passed" or checked["native_result_sha256"] != saved["validation"]["native_result_sha256"]:
            raise ValueError("native inventory/result no longer matches passed native gate")
        initial[name], sources[name] = [], []
        for record in saved["records"]:
            path = directory / "workspace/data" / f"repeat{record['repeat']}.part0001.log"
            blocks = energy_blocks(path.read_text())
            if [block["values"] for block in blocks] != [block["values"] for block in record["energy_blocks"]]:
                raise ValueError("native energy records differ from strict comparison")
            initial[name].append(blocks[0])
            sources[name].append({"repeat": record["repeat"], "native_log": str(path), "sha256": sha(path)})
    result = {"schema": "fs2-gromacs-cuda-mpi-printed-range-supplement/v1",
              "strict_comparison": str(args.strict_summary), "strict_sha256": sha(args.strict_summary),
              "strict_status_unchanged": original["status"], "strict_errors_retained": original["errors"],
              "sources": sources, "validator_sha256": sha(__file__),
              **printed_range_screen(initial["baseline"], initial["candidate"])}
    save(args.output / "printed-range-supplement.json", result)
    print(json.dumps({"screen_status": result["screen_status"], "strict_status_unchanged": result["strict_status_unchanged"],
                      "output": str(args.output / "printed-range-supplement.json")}))
    return 0 if result["screen_status"] == "within_bounds" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--ranks", type=int, choices=(1, 2), required=True)
    parser.add_argument("--strict-summary", type=Path,
                        help="Create an additive printed-range supplement; never overwrite the original strict comparison")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.strict_summary:
        return supplement(args)
    values = {}
    for name in ("baseline", "candidate"):
        out = args.output / name
        out.mkdir()
        values[name] = observation(getattr(args, name), args.fixture, args.ranks, out)
    result = {"schema": "fs2-gromacs-cuda-mpi-control/v1", "ranks": args.ranks, "baseline": values["baseline"],
              "candidate": values["candidate"], **compare(values["baseline"], values["candidate"]),
              "customer_ready": False, "validator_sha256": sha(__file__)}
    save(args.output / "summary.json", result)
    print(json.dumps({"status": result["status"], "errors": result["errors"], "output": str(args.output / "summary.json")}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
