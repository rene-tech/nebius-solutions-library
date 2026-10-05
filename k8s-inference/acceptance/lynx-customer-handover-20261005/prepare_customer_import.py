"""Prepare a tuned native import from an owner's latest terminal checkpoint.

Offline only: no API request, cloud mutation, cancellation, or data upload.
Use fresh public checkpoint metadata and a privately downloaded native tree.
The complete original history is retained separately from writable working
copies. The scientific TPR is never shortened, regenerated, or edited.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import sys
import tarfile
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
for relative in ("models/molecular-dynamics/gromacs/runtime", "components/control-plane/src", "catalog/runtime"):
    sys.path.insert(0, str(ROOT / relative))
from fs2_gromacs.contracts import canonical, normalize, relative_path  # noqa: E402
from fs2_serve.scientific_batch.gromacs_resume import continuation_parameters  # noqa: E402

MAX_BUNDLE_BYTES = 4 * 1024**3
MAX_NATIVE_FILES = 32766
PERFORMANCE_FLAGS = {
    "-nb": {"auto", "gpu", "cpu"},
    "-bonded": {"auto", "gpu", "cpu"},
    "-pme": {"auto", "gpu", "cpu"},
    "-update": {"auto", "gpu", "cpu"},
    "-pin": {"auto", "on", "off"},
    "-nstlist": {"100", "200", "300"},
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_source(*, choices, checkpoint_raw, parameters, source_operation,
                    job_id, source_engine_id, expected_tpr_sha256, expected_target_step):
    if (choices.get("operation_id"), choices.get("model_id")) != (source_operation, "gromacs"):
        raise ValueError("Checkpoint choices do not belong to the requested GROMACS operation")
    if choices.get("status") not in {"failed", "cancelled"}:
        raise ValueError("Source is not terminal; never interrupt an active customer job to prepare an import")
    selected = [item for item in choices["jobs"] if item["job_id"] == job_id]
    if len(selected) != 1:
        raise ValueError("Exactly one latest committed checkpoint is required for the chosen job")
    pointer = selected[0]["checkpoint"]
    if (len(checkpoint_raw), hashlib.sha256(checkpoint_raw).hexdigest()) != (
        pointer["size_bytes"], pointer["sha256"],
    ):
        raise ValueError("Checkpoint bytes do not match the latest public checkpoint pointer")
    checkpoint = json.loads(checkpoint_raw)
    state = checkpoint["state"]
    if (state.get("operation_id"), state.get("job_id")) != (source_operation, job_id):
        raise ValueError("Native checkpoint belongs to a different operation or job")
    active = state.get("active_step")
    if not active or (active.get("tpr_sha256"), active.get("target_step")) != (
        expected_tpr_sha256, expected_target_step,
    ):
        raise ValueError("Checkpoint TPR or full scientific target differs from the expected original")
    original = normalize(parameters)
    recipe = hashlib.sha256(canonical({"request": original, "job": job_id, "image": source_engine_id})).hexdigest()
    if recipe != state.get("recipe_sha256"):
        raise ValueError("Original parameters/engine do not match the frozen source recipe")
    commands = state.get("commands", [])
    completed_steps = [item["checkpoint_step"] for item in commands if type(item.get("checkpoint_step")) is int]
    if not completed_steps or not 0 < max(completed_steps) < expected_target_step:
        raise ValueError("Source has no incomplete native simulation to continue")
    return checkpoint, original, max(completed_steps)


def tuned_parameters(original, checkpoint, tuning, *, output_prefix, max_output_bytes):
    if set(tuning) != {"threads", "mdrun"} or set(tuning["mdrun"]) != set(PERFORMANCE_FLAGS):
        raise ValueError("Use the explicit qualified threads and six performance-only mdrun flags")
    if type(tuning["threads"]) is not int:
        raise ValueError("Qualified threads must be an integer")
    for name, value in tuning["mdrun"].items():
        if value not in PERFORMANCE_FLAGS[name]:
            raise ValueError("Tuning contains an unsupported or unqualified performance value")
    value = continuation_parameters(original, checkpoint, model_id="gromacs", max_wall_seconds=1209600)
    value["threads"] = tuning["threads"]
    value["max_output_bytes"] = max_output_bytes
    value["output_destination"] = "customer-bucket"
    value["output_prefix"] = relative_path(output_prefix)
    # Only the current simulation is tuned. Later scientific/analysis commands
    # keep their original arguments and order; completed preparation is skipped.
    step = value["jobs"][0]["steps"][0]
    if step["id"] != checkpoint["state"]["active_step"]["id"] or step["command"] != "mdrun":
        raise ValueError("First remaining command is not the checkpointed simulation")
    args = step["args"]
    for flag, setting in tuning["mdrun"].items():
        indices = [index for index, value in enumerate(args) if value == flag]
        if len(indices) > 1:
            raise ValueError("Original performance option is duplicated")
        if indices:
            index = indices[0]
            if index + 1 >= len(args) or not isinstance(args[index + 1], str):
                raise ValueError("Original performance option has no simple value")
            args[index + 1] = setting
        else:
            args.extend([flag, setting])
    return normalize(value)


def validated_files(checkpoint, native_root: Path):
    files = sorted(checkpoint["files"], key=lambda item: item["path"])
    names = [relative_path(item["path"]) for item in files]
    if not names or len(names) != len(set(names)) or 2 * len(names) + 2 > MAX_NATIVE_FILES:
        raise ValueError("Complete source plus preserved history exceeds the native file envelope")
    root = native_root.resolve(strict=True)
    for item in files:
        path = root / item["path"]
        if not stat.S_ISREG(path.lstat().st_mode) or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Native source must contain regular files inside the copied tree")
        if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValueError("Native source digest is invalid")
        if (path.stat().st_size, digest(path)) != (item["size_bytes"], item["sha256"]):
            raise ValueError("Copied native file differs from the latest committed checkpoint")
    return files


def add_bytes(archive, name, content):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(content), 0o600, 0
    archive.addfile(info, io.BytesIO(content))


def prepare(args):
    source_operation = str(UUID(args.source_operation))
    choices = json.loads(args.checkpoint_choices.read_text())
    raw = args.checkpoint.read_bytes()
    checkpoint, original, saved_step = verified_source(
        choices=choices, checkpoint_raw=raw, parameters=json.loads(args.original_parameters.read_text()),
        source_operation=source_operation, job_id=args.job_id, source_engine_id=args.source_engine_id,
        expected_tpr_sha256=args.expected_tpr_sha256, expected_target_step=args.expected_target_step,
    )
    parameters = tuned_parameters(original, checkpoint, json.loads(args.tuning.read_text()),
                                  output_prefix=args.output_prefix, max_output_bytes=args.max_output_bytes)
    files = validated_files(checkpoint, args.native_root)
    generation = checkpoint["state"]["generation"]
    history = f"source-history-{source_operation}-g{generation}"
    if any(item["path"] == history or item["path"].startswith(history + "/") for item in files):
        raise ValueError("Preserved history path collides with a source file")
    provenance = {
        "schema": "fs2-gromacs-customer-tuned-import/v1", "source_operation": source_operation,
        "source_job": args.job_id, "source_generation": generation, "source_step": saved_step,
        "source_checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
        "source_recipe_sha256": checkpoint["state"]["recipe_sha256"],
        "original_tpr_sha256": args.expected_tpr_sha256, "original_target_step": args.expected_target_step,
        "source_files": len(files), "source_bytes": sum(item["size_bytes"] for item in files),
        "history_prefix": history, "tuning": json.loads(args.tuning.read_text()),
        "original_parameters_sha256": hashlib.sha256(canonical(original)).hexdigest(),
        "new_parameters_sha256": hashlib.sha256(canonical(parameters)).hexdigest(),
        "science_change": "None: exact original TPR and full target retained; performance-only arguments changed.",
        "submission": "Not submitted. Upload under the actual owner's key and use ordinary public run-workflow.",
    }
    if 2 * provenance["source_bytes"] + len(raw) + len(canonical(provenance)) > args.max_output_bytes:
        raise ValueError("Complete working files plus history exceed requested workspace bytes")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with (args.output / "input.tar.gz").open("xb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w|") as archive:
                for item in files:
                    path = args.native_root / item["path"]
                    for name in (item["path"], f"{history}/{item['path']}"):
                        info = tarfile.TarInfo(name)
                        info.size, info.mode, info.mtime = item["size_bytes"], 0o600, 0
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                        if digest(path) != item["sha256"]:
                            raise ValueError("Native source changed while preparing the import")
                add_bytes(archive, f"{history}/source-checkpoint.json", raw)
                add_bytes(archive, f"{history}/source-provenance.json", canonical(provenance))
    if (args.output / "input.tar.gz").stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Full import exceeds bundle limit; use per-file continuation, never omit customer history")
    for name, value in (("parameters.json", parameters), ("provenance.json", provenance)):
        with (args.output / name).open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    return {"prepared": True, "source_files": len(files), "source_bytes": provenance["source_bytes"],
            "saved_step": saved_step, "full_target_step": args.expected_target_step,
            "input_sha256": digest(args.output / "input.tar.gz"), "submitted": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("checkpoint-choices", "checkpoint", "original-parameters", "native-root", "tuning", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("source-operation", "job-id", "source-engine-id", "expected-tpr-sha256", "output-prefix"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--expected-target-step", type=int, required=True)
    parser.add_argument("--max-output-bytes", type=int, default=4 * 1024**3)
    print(json.dumps(prepare(parser.parse_args()), sort_keys=True))


if __name__ == "__main__":
    main()
