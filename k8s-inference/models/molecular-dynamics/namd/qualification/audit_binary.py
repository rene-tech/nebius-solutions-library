"""Read-only CUDA binary inventory; compiled targets are not runtime dispatch proof."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


def command(argv):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=120)
    return {"command": argv, "exit_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}


def summarize(elf, ptx, symbols):
    kernels = {}
    for architecture, record in symbols.items():
        entries = [line.split()[-1] for line in record["stdout"].splitlines()
                   if "STT_FUNC" in line and "STO_ENTRY" in line]
        namd = [name for name in entries if re.search(r"bondedForcesKernel|computeNonbondedIndex|CudaBond|CudaPatch", name)]
        kernels[architecture] = {"entry_count": len(entries), "namd_marker_entry_count": len(namd),
                                 "namd_marker_examples": namd[:12],
                                 "curand_marker_entry_count": sum("curand" in name.lower() for name in entries)}
    return {"elf_architecture_counts": dict(Counter(re.findall(r"\.(sm_\d+[a-z]?)\.cubin", elf))),
            "ptx_target_counts": dict(Counter(re.findall(r"\.(sm_\d+[a-z]?)\.ptx", ptx))),
            "selected_architecture_symbol_inventory": kernels,
            "runtime_dispatch_proven": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cuobjdump", default="cuobjdump")
    parser.add_argument("--architecture", action="append")
    args = parser.parse_args()
    with args.binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    records = {"version": command([args.cuobjdump, "--version"]),
               "elf": command([args.cuobjdump, "--list-elf", str(args.binary)]),
               "ptx": command([args.cuobjdump, "--list-ptx", str(args.binary)])}
    symbols = {arch: command([args.cuobjdump, "--dump-elf-symbols", "--gpu-architecture", arch, str(args.binary)])
               for arch in (args.architecture or ["sm_86", "sm_89", "sm_90"])}
    report = {"binary": str(args.binary), "binary_sha256": digest,
              "recorded_at": datetime.now(timezone.utc).isoformat(), "commands": records,
              "symbols": symbols,
              "summary": summarize(records["elf"]["stdout"], records["ptx"]["stdout"], symbols)}
    report["status"] = "passed" if all(row["exit_code"] == 0 for row in [*records.values(), *symbols.values()]) else "incomplete"
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(args.output), "binary_sha256": digest,
                      "status": report["status"], "summary": report["summary"]}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
