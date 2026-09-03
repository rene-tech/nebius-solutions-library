#!/usr/bin/env python3
"""Translate the typed RFdiffusion batch envelope to the pinned upstream CLI."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any, NoReturn


REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/rfdiffusion-upstream-base-parameters/v1"
CHECKPOINT_SHA256 = "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca"


def fail(message: str) -> NoReturn:
    raise SystemExit("RFdiffusion typed runtime rejected request: " + message)


def load_request() -> dict[str, Any]:
    path = Path(os.environ.get("FS2_REQUEST_PATH", "/var/run/fs2/request.json"))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"request is unavailable or invalid: {exc.__class__.__name__}")
    if not isinstance(value, dict) or value.get("schema") != REQUEST_SCHEMA:
        fail("unexpected request schema")
    parameters = value.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("schema") != PARAMETER_SCHEMA:
        fail("unexpected parameter schema")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def value_after(prefix: str) -> str:
    for argument in sys.argv[1:]:
        if argument.startswith(prefix):
            return argument[len(prefix):]
    fail(f"adapter wrapper omitted {prefix[:-1]}")


def typed_contigs(items: Any) -> str:
    if not isinstance(items, list) or not items:
        fail("contigs must be a nonempty array")
    tokens: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            fail("contig is not an object")
        if item.get("kind") == "generated":
            lower, upper = item.get("minimum_length"), item.get("maximum_length")
            if not isinstance(lower, int) or not isinstance(upper, int) or not 1 <= lower <= upper <= 512:
                fail("generated contig bounds are invalid")
            tokens.append(f"{lower}-{upper}")
        elif item.get("kind") == "motif":
            chain, start, end = item.get("chain"), item.get("start"), item.get("end")
            if not isinstance(chain, str) or len(chain) != 1 or not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end <= 9999:
                fail("motif contig is invalid")
            tokens.append(f"{chain}{start}-{end}")
        else:
            fail("unsupported contig kind")
    return "/0 ".join(tokens)


def main() -> None:
    request = load_request()
    parameters = request["parameters"]
    output = value_after("inference.output_prefix=")
    seed_text = value_after("inference.seed=")
    try:
        seed = int(seed_text)
    except ValueError:
        fail("seed is not an integer")
    if not 0 <= seed <= 2_147_483_647:
        fail("seed is outside int32 range")
    checkpoint = Path(os.environ.get("FS2_RFDIFFUSION_CHECKPOINT", "/opt/fs2/models/Base_ckpt.pt"))
    if not checkpoint.is_file() or checkpoint.is_symlink() or file_sha256(checkpoint) != CHECKPOINT_SHA256:
        fail("Base checkpoint identity mismatch")
    steps = parameters.get("diffusion_steps")
    if not isinstance(steps, int) or not 10 <= steps <= 200:
        fail("diffusion_steps is invalid")
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "run_inference.py",
        f"inference.output_prefix={output}",
        "inference.num_designs=1",
        f"inference.design_startnum={seed}",
        "inference.deterministic=True",
        "inference.write_trajectory=False",
        f"inference.ckpt_override_path={checkpoint}",
        f"diffuser.T={steps}",
        f"contigmap.contigs=[{typed_contigs(parameters.get('contigs'))}]",
        "inference.schedule_directory_path=/work/schedules",
        "hydra.run.dir=/tmp/fs2-hydra",
        "hydra.output_subdir=null",
        "hydra.job.chdir=False",
    ]
    motifs = [item for item in parameters["contigs"] if item.get("kind") == "motif"]
    if motifs:
        input_pdb = Path(os.environ.get("FS2_RFDIFFUSION_INPUT_PDB", "/workspace/inputs/target_structure.pdb"))
        if not input_pdb.is_file() or input_pdb.is_symlink():
            fail("motif request requires a staged target PDB")
        argv.append(f"inference.input_pdb={input_pdb}")
    hotspots = parameters.get("hotspots")
    if hotspots:
        if not isinstance(hotspots, list):
            fail("hotspots must be an array")
        values = []
        for item in hotspots:
            if not isinstance(item, dict) or not isinstance(item.get("chain"), str) or not isinstance(item.get("residue"), int):
                fail("hotspot is invalid")
            values.append(f"{item['chain']}{item['residue']}")
        argv.append("ppi.hotspot_res=[" + ",".join(values) + "]")
    sys.argv = argv
    runpy.run_path("/opt/rfdiffusion/scripts/run_inference.py", run_name="__main__")


if __name__ == "__main__":
    main()
