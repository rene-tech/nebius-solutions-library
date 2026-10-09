"""Prepare public-fixture prompts for the unchanged, seeded customer agent."""
import argparse
import hashlib
import json
from pathlib import Path
from prepare import parameters, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--workspace-inputs", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    cases = []
    rows = json.loads((args.fixtures / "suite.json").read_text())["cases"]
    # Exercise smaller cases while the REST cohort's large systems are running.
    for row in sorted(rows, key=lambda row: row["tpr_bytes"]):
        case = row["id"]
        root = args.workspace_inputs / case
        if hashlib.sha256((root / "input.tar.gz").read_bytes()).hexdigest() != row["bundle_sha256"]:
            raise ValueError("Isolated workspace fixture differs from source provenance")
        params = parameters(row)
        params["output_prefix"] = f"runs/fs2-mpinat-agent-b-20261003/{case}"
        save(root / "parameters-b.json", params)
        cases.append({"case_id": "mpinat-" + case,
            "prompt": f"Run the public Max Planck GROMACS {row['name']} performance benchmark on Scientific AI. "
            f"My inputs are /workspace/inputs/mpinat/{case}/input.tar.gz (unchanged upstream TPR) and "
            f"/workspace/inputs/mpinat/{case}/parameters-b.json (three 10,000-step timing repetitions). "
            "Use the installed GROMACS skill, live schema and hosted MCP workflow, not local MD. "
            "Use the supplied parameters and preserve their physics, including constraints, PME and free-energy settings. "
            "These timings include tuning/warmup: no forced counter reset while PME tuning is active. "
            "These identical starts are performance repeats, not independent scientific replicas. "
            "Put receipts, logs, verified artifacts and a report under {output}. "
            f"Use idempotency key fs2-mpinat-agent-b-{case}-20261003 and the supplied bucket prefix. "
            "Wait for durable results, retrieve and verify them, then give the operation ID, requested/executed/committed "
            "steps, native ns/day and wall time for each repeat, plus usable result links. "
            "Do not invent missing measurements or declare a short failed operation to have done no computation. "
            "On a terminal failure preserve its native diagnostics; do not resubmit under another key or silently change "
            "the protocol. If still running when you must return, give an explicit incomplete handoff and operation ID."})
    # This deliberately invalid native file is an expected negative-path check,
    # not a scientific case and never counted in benchmark success totals.
    negative = parameters({"id": "benchmem"})
    negative["jobs"][0]["steps"] = [negative["jobs"][0]["steps"][0],
        {"id": "expected-failure", "command": "check", "args": ["-f", "deliberately-missing.xtc"]}]
    negative["output_prefix"] = "runs/fs2-mpinat-negative-20261003"
    save(args.workspace_inputs / "benchmem" / "negative-diagnostics.json", negative)
    cases.insert(0, {"case_id": "expected-native-failure", "prompt":
        "Check Scientific AI's native failure reporting using the installed GROMACS skill and hosted MCP workflow. "
        "Use /workspace/inputs/mpinat/benchmem/input.tar.gz and /workspace/inputs/mpinat/benchmem/negative-diagnostics.json. "
        "This intentionally asks gmx check to read deliberately-missing.xtc after a valid convert-tpr step. "
        "The expected result is a failed operation with downloadable native diagnostic logs, not scientific success. "
        "Use the key fs2-mpinat-negative-20261003 and supplied bucket prefix. Keep all receipts, verified diagnostics "
        "and a report under {output}. Do not fix the missing file or retry under a new key. Report the native error "
        "and whether the logs were actually downloadable and hash-verified."})
    save(args.manifest, {"cases": cases})
    print(json.dumps({"scientific_cases": len(rows), "expected_negative_cases": 1}))


if __name__ == "__main__":
    main()
