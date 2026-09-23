"""Exercise the exact released LibreChat CLI image against the real MCP gateway.

This does not replace the user's recording endpoint or claim browser-agent UX.
Authentication is passed in process environment, never in command arguments.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--model", choices=["gromacs", "gromacs-mpi"], default="gromacs"
    )
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        raise ValueError("Pin the tested workbench image digest.")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        **os.environ,
        "SCIENTIFIC_MODELS_API_KEY": json.loads(args.key_file.read_text())["secret"],
        "SCIENTIFIC_MODELS_MCP_URL": "https://89.169.99.188/mcp",
    }
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--entrypoint",
        "/opt/scientific-client/bin/python",
        "--env",
        "SCIENTIFIC_MODELS_API_KEY",
        "--env",
        "SCIENTIFIC_MODELS_MCP_URL",
        "--mount",
        f"type=bind,src={args.fixture.resolve()},dst=/qualification/input,readonly",
        "--mount",
        f"type=bind,src={args.output.resolve()},dst=/qualification/receipt",
        args.image,
        "/opt/bionemo/invoke-scientific-batch.py",
        "--model",
        args.model,
        "--tool",
        "submit_gromacs_mpi_workflow"
        if args.model == "gromacs-mpi"
        else "submit_gromacs_workflow",
        "--operation",
        "run-workflow",
        "--source",
        "/qualification/input/input.tar.gz",
        "--parameters",
        "/qualification/input/request.json",
        "--entry-name",
        "gromacs-inputs",
        "--semantic-type",
        "gromacs-input-bundle/v1",
        "--media-type",
        "application/x-tar",
        "--compression",
        "gzip",
        "--output",
        "/qualification/receipt",
        "--idempotency-key",
        args.idempotency_key,
        "--display-name",
        "GROMACS exact-workbench-image qualification",
        "--wait-seconds",
        "1800",
        "--poll-seconds",
        "5",
    ]
    raise SystemExit(subprocess.run(command, env=env).returncode)


if __name__ == "__main__":
    main()
