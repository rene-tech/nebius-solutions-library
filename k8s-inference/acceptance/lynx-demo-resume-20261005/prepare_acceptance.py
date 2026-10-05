"""Prepare an immutable late-checkpoint fixture; no API, S3 or GPU mutations.

The operator supplies an already copied customer checkpoint. All closed native
files remain byte-identical under source-history/, including wrapper logs. A
separate TPR changes only the finite acceptance horizon, never the original.
"""

import argparse
from decimal import Decimal
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models/molecular-dynamics/gromacs/runtime"))
from fs2_gromacs.contracts import canonical, normalize, relative_path  # noqa: E402

SOURCE_OPERATION = "aa502153-3c75-422c-8040-82461fdbfcaa"
SOURCE_STEP = 13963440
SOURCE_GENERATION = 71
TPR_SHA256 = "e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10"
DT_PS = Decimal("0.002")
PREFIX = "runs/fs2-lynx-final-20261005"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def native_step(manifest):
    commands = manifest["state"].get("commands", [])
    steps = [row["checkpoint_step"] for row in commands
             if type(row.get("checkpoint_step")) is int]
    if not steps:
        raise ValueError("Source has no committed native checkpoint step")
    return max(steps)


def validate_source(manifest, native_root, *, tpr_path, checkpoint_path):
    state = manifest["state"]
    if (manifest.get("schema"), state.get("operation_id"), state.get("generation"), native_step(manifest)) != (
        "fs2-serve.nebius.ai/gromacs-customer-checkpoint/v1", SOURCE_OPERATION,
        SOURCE_GENERATION, SOURCE_STEP,
    ):
        raise ValueError("Not the approved original six-hour late checkpoint")
    files = manifest["files"]
    names = [relative_path(row["path"]) for row in files]
    if not files or len(names) != len(set(names)) or len(names) > 32760:
        raise ValueError("Source inventory is empty, duplicated or exceeds native capacity")
    indexed = {row["path"]: row for row in files}
    tpr = indexed[tpr_path]
    if tpr["sha256"] != TPR_SHA256 or state["active_step"]["tpr_sha256"] != TPR_SHA256:
        raise ValueError("Original TPR differs from the approved immutable source")
    if checkpoint_path not in indexed or not checkpoint_path.endswith(".cpt"):
        raise ValueError("Native restart checkpoint is absent")
    native_root = Path(native_root).resolve(strict=True)
    for row in files:
        path = native_root / row["path"]
        if not stat.S_ISREG(path.lstat().st_mode) or not path.resolve(strict=True).is_relative_to(native_root):
            raise ValueError("Source must contain only regular, contained files")
        if (path.stat().st_size, sha(path)) != (row["size_bytes"], row["sha256"]):
            raise ValueError("Copied source file does not match its committed SHA-256/size")
    return sorted(files, key=lambda row: row["path"])


def target_step(additional_ns, *, init_step=0):
    additional = Decimal(str(additional_ns)) * 1000 / DT_PS
    if not additional.is_finite() or additional != additional.to_integral_value() or additional <= 0:
        raise ValueError("Acceptance duration must be a positive whole number of 2-fs steps")
    target = SOURCE_STEP + int(additional)
    if not 0 <= init_step < SOURCE_STEP or target >= 500000000:
        raise ValueError("Use a bounded acceptance horizon inside the original one-microsecond request")
    # GROMACS load_checkpoint subtracts the saved step from init-step+nsteps.
    return target, target - init_step


def parameters(*, tpr_path, checkpoint_path, additional_ns, label, nstlist,
               threads=8, pin="auto", bootstrap_seconds=300, max_output_bytes=4 * 1024**3):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", label):
        raise ValueError("Use a distinct path-safe acceptance label")
    if nstlist not in (100, 200, 300) or pin not in ("auto", "on", "off"):
        raise ValueError("Use an explicitly measured neighbor-list/pinning recipe")
    if not 60 <= bootstrap_seconds <= 300:
        raise ValueError("Bootstrap is a labelled short expected timeout, not a long soak")
    target, nsteps = target_step(additional_ns)
    source_tpr = str(PurePosixPath("source-history") / relative_path(tpr_path))
    source_cpt = str(PurePosixPath("source-history") / relative_path(checkpoint_path))
    return normalize({
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "jobs": [{"id": "late-resume", "steps": [
            {"id": "acceptance-horizon", "command": "convert-tpr",
             "args": ["-s", source_tpr, "-o", "acceptance.tpr", "-nsteps", str(nsteps)],
             "expected_outputs": ["acceptance.tpr"]},
            {"id": "verify-tpr", "command": "check",
             "args": ["-s1", source_tpr, "-s2", "acceptance.tpr", "-tol", "0", "-abstol", "0"]},
            {"id": "production", "command": "mdrun", "restart_checkpoint": source_cpt,
             "args": ["-s", "acceptance.tpr", "-deffnm", "continued", "-nb", "gpu",
                      "-bonded", "gpu", "-pme", "auto", "-update", "auto",
                      "-pin", pin, "-nstlist", str(nstlist)]},
            {"id": "join-energy", "command": "eneconv",
             "args": ["-f", {"files": "continued.part*.edr"}, "-o", "continued.edr"],
             "expected_outputs": ["continued.edr"]},
            {"id": "thermodynamics", "command": "energy",
             "args": ["-f", "continued.edr", "-o", "thermodynamics.xvg"],
             "stdin": "Potential\nKinetic-En.\nTotal-Energy\nTemperature\nPressure\n0\n",
             "expected_outputs": ["thermodynamics.xvg"]},
            {"id": "join-trajectory", "command": "trjcat",
             "args": ["-f", {"files": "continued.part*.xtc"}, "-o", "continued.xtc"],
             "expected_outputs": ["continued.xtc"]},
            {"id": "check-trajectory", "command": "check", "args": ["-f", "continued.xtc"]},
        ]}],
        "threads": threads, "checkpoint_minutes": 5, "segment_minutes": 5,
        "max_wall_seconds": bootstrap_seconds, "max_output_bytes": max_output_bytes,
        "output_destination": "customer-bucket", "output_prefix": f"{PREFIX}/{label}",
    }), target


def add_bytes(archive, name, content):
    entry = tarfile.TarInfo(name)
    entry.size, entry.mode, entry.mtime = len(content), 0o600, 0
    archive.addfile(entry, io.BytesIO(content))


def prepare(args):
    manifest_path = args.source_manifest
    if sha(manifest_path) != args.source_manifest_sha256:
        raise ValueError("Operator-frozen source manifest digest differs")
    manifest = json.loads(manifest_path.read_text())
    files = validate_source(manifest, args.native_root, tpr_path=args.tpr_path,
                            checkpoint_path=args.checkpoint_path)
    request, target = parameters(tpr_path=args.tpr_path, checkpoint_path=args.checkpoint_path,
        additional_ns=args.additional_ns, label=args.label, nstlist=args.nstlist,
        threads=args.threads, pin=args.pin, bootstrap_seconds=args.bootstrap_seconds,
        max_output_bytes=args.max_output_bytes)
    provenance = {
        "schema": "fs2-lynx-late-import/v1", "source_operation": SOURCE_OPERATION,
        "source_generation": SOURCE_GENERATION, "source_step": SOURCE_STEP,
        "source_manifest_sha256": args.source_manifest_sha256,
        "original_tpr_sha256": TPR_SHA256, "original_target_step": 500000000,
        "target_step": target, "additional_ns": str(args.additional_ns), "dt_ps": str(DT_PS),
        "tpr_source_path": args.tpr_path, "checkpoint_source_path": args.checkpoint_path,
        "history_prefix": "source-history", "source_files": files,
        "source_bytes": sum(row["size_bytes"] for row in files),
        "science_change": "Separate finite nsteps horizon only; original TPR/history remain unmodified.",
        "bootstrap": "Expected execution-budget stop solely to establish demo-owned public :resume source.",
        "topology_equivalence": "Require gmx check -s1/-s2 with zero tolerances; only nsteps may differ.",
    }
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / "input.tar.gz").open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for row in files:
                    source = args.native_root / row["path"]
                    entry = tarfile.TarInfo(f"source-history/{row['path']}")
                    entry.size, entry.mode, entry.mtime = row["size_bytes"], 0o600, 0
                    with source.open("rb") as handle:
                        archive.addfile(entry, handle)
                    if sha(source) != row["sha256"]:
                        raise ValueError("Source changed while assembling immutable import bundle")
                add_bytes(archive, "source-checkpoint-manifest.json", manifest_path.read_bytes())
                add_bytes(archive, "source-import-provenance.json", canonical(provenance))
    if (args.output / "input.tar.gz").stat().st_size > 4 * 1024**3:
        raise ValueError("Import exceeds the existing public bundle limit; do not silently drop history")
    write_json(args.output / "request.json", request)
    write_json(args.output / "fixture.json", {
        **provenance, "input_sha256": sha(args.output / "input.tar.gz"),
        "request_sha256": sha(args.output / "request.json"),
        "minimum_native_seconds": args.minimum_native_seconds,
        "minimum_delivered_ns_per_day": args.minimum_delivered_ns_per_day,
        "label": args.label, "operator_authorized_customer_copy": True,
    })
    print(json.dumps({"prepared": True, "source_files": len(files),
                      "source_bytes": provenance["source_bytes"], "target_step": target}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-manifest", "native-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--tpr-path", default="simulation.tpr")
    parser.add_argument("--checkpoint-path", default="fs2-production.cpt")
    parser.add_argument("--additional-ns", type=Decimal, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--nstlist", type=int, choices=(100, 200, 300), required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--pin", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--bootstrap-seconds", type=int, default=300)
    parser.add_argument("--max-output-bytes", type=int, default=4 * 1024**3)
    parser.add_argument("--minimum-native-seconds", type=float, default=0)
    parser.add_argument("--minimum-delivered-ns-per-day", type=float, default=200)
    prepare(parser.parse_args())


if __name__ == "__main__":
    main()
