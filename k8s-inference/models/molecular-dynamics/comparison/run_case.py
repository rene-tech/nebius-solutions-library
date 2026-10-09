"""Run one archived canonical case through its pinned native worker or hosted App.

Only Python's standard library and Docker are required locally. Native mode also
needs NVIDIA Container Toolkit and one compatible GPU. Registry access and any
engine licence must already be supplied by the operator; this script accepts no
new licence and never substitutes a different engine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

ENGINES = ("gromacs", "namd", "amber", "lammps")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def describe(case: Path, engine: str, mode: str, output: Path, gpu: str) -> tuple[dict, list[str]]:
    manifest = json.loads((case / "case.json").read_text())
    if manifest["schema"] != "fs2-four-engine-alanine-case/v1" or set(manifest["engines"]) != set(ENGINES):
        raise ValueError("Expected a complete four-engine canonical case manifest.")
    entry = manifest["engines"][engine]
    fixture = case / "inputs" / engine
    for filename in ("input.tar.gz", "request.json"):
        if digest(fixture / filename) != entry["sha256"][filename]:
            raise ValueError(f"Archived {engine} {filename} changed; do not run an altered comparison silently.")
    request = json.loads((fixture / "request.json").read_text())
    if len(request["jobs"]) != 1:
        raise ValueError("This canonical comparison expects exactly one job per engine.")
    image = entry["worker_image"] if mode == "native" else manifest["client_image"]
    if "@sha256:" not in image:
        raise ValueError("An immutable worker/client image digest is required.")
    identity = {"engine": engine, "mode": mode, "image": image, "inputs": entry["sha256"],
                "job_id": request["jobs"][0]["id"]}
    command = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}"]
    if mode == "native":
        command += ["--gpus", "device=" + gpu, "--shm-size", "2g", "--entrypoint", "python3",
                    "--mount", f"type=bind,src={output.resolve()},dst=/case", image,
                    "-m", f"fs2_{engine}.worker", "--request", "/case/request.json",
                    "--workspace", "/case", "--job-id", identity["job_id"],
                    "--operation-id", "<saved-operation-id>", "--checkpoint-mode", "local"]
    else:
        command += ["--entrypoint", "/opt/scientific-client/bin/python",
                    "--env", "SCIENTIFIC_MODELS_API_KEY", "--env", "SCIENTIFIC_MODELS_MCP_URL",
                    "--mount", f"type=bind,src={fixture.resolve()},dst=/input,readonly",
                    "--mount", f"type=bind,src={output.resolve()},dst=/receipt", image,
                    "/opt/bionemo/invoke-scientific-batch.py", "--model", engine,
                    "--tool", f"submit_{engine}_workflow", "--operation", "run-workflow",
                    "--source", "/input/input.tar.gz", "--parameters", "/input/request.json",
                    "--entry-name", engine + "-inputs", "--semantic-type", engine + "-input-bundle/v1",
                    "--media-type", "application/x-tar", "--compression", "gzip",
                    "--output", "/receipt", "--idempotency-key", "<saved-operation-id>",
                    "--display-name", f"{engine.upper()} canonical ff14SB/TIP3P comparison",
                    "--wait-seconds", "1800", "--poll-seconds", "5"]
        identity["endpoint"] = os.environ.get("SCIENTIFIC_MODELS_MCP_URL", "")
        identity["caller_fingerprint"] = hashlib.sha256(
            os.environ.get("SCIENTIFIC_MODELS_API_KEY", "").encode()).hexdigest()
    return identity, command


def run(args) -> int:
    if args.key_file:
        os.environ["SCIENTIFIC_MODELS_API_KEY"] = json.loads(args.key_file.read_text())["secret"]
    if args.mcp_url:
        os.environ["SCIENTIFIC_MODELS_MCP_URL"] = args.mcp_url
    identity, command = describe(args.case, args.engine, args.mode, args.output, args.gpu)
    if args.describe:
        print(json.dumps({"identity": identity, "command": command}, indent=2))
        return 0
    if shutil.which("docker") is None:
        raise RuntimeError("Missing dependency: Docker CLI/daemon. No simulation was submitted.")
    if args.mode == "hosted" and any(not os.environ.get(name) for name in (
        "SCIENTIFIC_MODELS_API_KEY", "SCIENTIFIC_MODELS_MCP_URL"
    )):
        raise ValueError("Hosted mode needs SCIENTIFIC_MODELS_API_KEY and SCIENTIFIC_MODELS_MCP_URL.")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = args.output / "reproduction.json"
    if marker.exists():
        record = json.loads(marker.read_text())
        if record["identity"] != identity:
            raise ValueError("Output directory belongs to a different case, image, mode or caller.")
    else:
        if any(args.output.iterdir()):
            raise ValueError("Use an empty output directory; existing results are never overwritten.")
        record = {"identity": identity, "operation_id": str(uuid4())}
        with marker.open("x") as stream:
            marker.chmod(0o600)
            json.dump(record, stream, indent=2)
    if args.mode == "native":
        for name in ("input.tar.gz", "request.json"):
            target = args.output / name
            if target.exists():
                if digest(target) != identity["inputs"][name]:
                    raise ValueError("Native resume input changed; preserve this attempt unchanged.")
            else:
                shutil.copyfile(args.case / "inputs" / args.engine / name, target)
    command[command.index("<saved-operation-id>")] = (
        record["operation_id"] if args.mode == "native" else
        f"md-alanine-{args.engine}-reproduce-{record['operation_id']}")
    # A resume reuses the original native operation or customer idempotency key.
    # Never infer scientific success merely from this process exit code.
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--engine", choices=ENGINES, required=True)
    parser.add_argument("--mode", choices=("hosted", "native"), default="hosted")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpu", default="0", help="One native Docker GPU index or UUID, never all devices.")
    parser.add_argument("--key-file", type=Path, help="Optional private JSON with a secret field; never copied to outputs.")
    parser.add_argument("--mcp-url", help="Hosted MCP URL; alternatively SCIENTIFIC_MODELS_MCP_URL.")
    parser.add_argument("--describe", action="store_true", help="Verify inputs and print the command without executing.")
    raise SystemExit(run(parser.parse_args()))
