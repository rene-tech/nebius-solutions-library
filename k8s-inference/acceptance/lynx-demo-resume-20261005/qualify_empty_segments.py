"""CPU-only regression of the actual late trajectory on an immutable worker.

Mounts the copied native inputs read-only. It does not use an API key, launch
GPU work, or alter customer data. The unfiltered native failure is deliberate
control evidence; the explicitly filtered command and native output check must
succeed. This is feature qualification, not public or performance qualification.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


PROBE = r'''
import hashlib, json, pathlib, re, subprocess
import fs2_gromacs.contracts as contracts
import fs2_gromacs.worker as worker

root = pathlib.Path("/inputs")
out = pathlib.Path("/results")
pattern = "md.part*.xtc"
matches = sorted(root.glob(pattern))
empty = [path for path in matches if path.stat().st_size == 0]
assert matches and empty and len(empty) < len(matches), "Need real empty and nonempty parts"
legacy = worker.expand_args(["-f", {"files": pattern}, "-o", "/results/legacy.xtc"], root)
selected = worker.expand_args(["-f", {"files": pattern, "nonempty": True}, "-o", "/results/filtered.xtc"], root)
assert len(legacy) == len(matches) + 3
assert len(selected) == len(matches) - len(empty) + 3

def run(name, args):
    result = subprocess.run([worker.DEFAULT_GMX, *args], cwd=root, capture_output=True, text=True, timeout=120)
    (out / (name + ".log")).write_text(result.stdout + result.stderr)
    return result

version = run("version", ["--version"])
assert version.returncode == 0
before = run("legacy", ["trjcat", *legacy])
assert before.returncode != 0, "Unfiltered control unexpectedly succeeded"
assert "0 atom coords read" in before.stderr, "Control did not reproduce the actual empty-XTC failure"
after = run("nonempty", ["trjcat", *selected])
assert after.returncode == 0, "Explicit nonempty selection failed"
checked = run("trajectory-check", ["check", "-f", "/results/filtered.xtc"])
assert checked.returncode == 0, "Native output validation failed"
validation = checked.stdout + checked.stderr
atoms = re.search(r"# Atoms\s+(\d+)", validation)
frames = re.search(r"^Coords\s+(\d+)\s+(\d+)", validation, re.M)
assert atoms and int(atoms.group(1)) == 185486, "Changed or missing native particle count"
assert frames and (int(frames.group(1)), int(frames.group(2))) == (28, 1000), "Missing frames or changed output cadence"
data = out / "filtered.xtc"
result = {
    "status": "passed", "total_parts": len(matches), "empty_parts": len(empty),
    "selected_parts": len(matches) - len(empty),
    "legacy_exit_code": before.returncode, "filtered_exit_code": after.returncode,
    "native_check_exit_code": checked.returncode,
    "particles": int(atoms.group(1)), "coordinate_frames": int(frames.group(1)),
    "frame_interval_ps": int(frames.group(2)),
    "worker_sha256": hashlib.sha256(pathlib.Path(worker.__file__).read_bytes()).hexdigest(),
    "contracts_sha256": hashlib.sha256(pathlib.Path(contracts.__file__).read_bytes()).hexdigest(),
    "output_bytes": data.stat().st_size,
    "output_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
}
assert result["output_bytes"] > 0
print(json.dumps(result))
'''


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Native input must contain only contained regular files")
        if path.is_file():
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            result[str(path.relative_to(root))] = {
                "size_bytes": path.stat().st_size,
                "sha256": digest,
            }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", args.image):
        parser.error("Use the immutable worker digest, not a mutable image tag")
    source = args.native_root.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve(strict=True)
    before = inventory(source)
    started = time.monotonic()
    result = subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--cpus", "2", "--memory", "2g",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={source},dst=/inputs,readonly",
        "--mount", f"type=bind,src={output},dst=/results",
        "--env", "PYTHONPATH=/opt/fs2/gromacs", "--entrypoint", "python3", args.image,
        "-c", PROBE,
    ], capture_output=True, text=True, timeout=360)
    (output / "probe.stdout").write_text(result.stdout)
    (output / "probe.stderr").write_text(result.stderr)
    after = inventory(source)
    if result.returncode != 0:
        raise RuntimeError(f"Native empty-part qualification failed; see {output}")
    if before != after:
        raise RuntimeError("Original copied native files changed")
    receipt = json.loads(result.stdout)
    receipt.update(image=args.image, wall_seconds=time.monotonic() - started,
                   preserved_files=len(before), preserved_bytes=sum(row["size_bytes"] for row in before.values()),
                   source_inventory=before, source_read_only=True, gpu_used=False,
                   scope="Native file-selection feature; not public performance or recovery qualification")
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: value for key, value in receipt.items() if key != "source_inventory"}, indent=2))


if __name__ == "__main__":
    main()
