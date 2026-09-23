"""Exercise the exact released LibreChat CLI image against the real MCP gateway.

This does not replace the user's recording endpoint or claim browser-agent UX.
Authentication is passed in process environment, never in command arguments.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

MODEL_CONTRACTS = {
    "gromacs": ("submit_gromacs_workflow", "gromacs"),
    "gromacs-mpi": ("submit_gromacs_mpi_workflow", "gromacs"),
    "lammps": ("submit_lammps_workflow", "lammps"),
    "namd": ("submit_namd_workflow", "namd"),
    "amber": ("submit_amber_workflow", "amber"),
}


def transport_parameters(fixture, output, destination):
    """Persist a transport-only variant; never mutate the scientific fixture."""
    original = (fixture / "request.json").read_bytes()
    request = json.loads(original)
    previous = request.get("output_destination", "customer-bucket")
    request["output_destination"] = destination
    encoded = (json.dumps(request, indent=2) + "\n").encode()
    (output / "request-transport.json").write_bytes(encoded)
    (output / "transport-variant.json").write_text(json.dumps({
        "original_request_sha256": hashlib.sha256(original).hexdigest(),
        "derived_request_sha256": hashlib.sha256(encoded).hexdigest(),
        "changed_fields": {"output_destination": {"from": previous, "to": destination}},
        "native_input_archive_changed": False,
        "scientific_parameters_changed": False,
    }, indent=2) + "\n")
    return "/qualification/receipt/request-transport.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--model", choices=sorted(MODEL_CONTRACTS), default="gromacs"
    )
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--output-destination", choices=["customer-bucket", "platform-artifacts"])
    args = parser.parse_args()
    tool, input_family = MODEL_CONTRACTS[args.model]
    if "@sha256:" not in args.image:
        raise ValueError("Pin the tested workbench image digest.")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    parameters = (
        transport_parameters(args.fixture, args.output, args.output_destination)
        if args.output_destination else "/qualification/input/request.json"
    )
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
        tool,
        "--operation",
        "run-workflow",
        "--source",
        "/qualification/input/input.tar.gz",
        "--parameters",
        parameters,
        "--entry-name",
        f"{input_family}-inputs",
        "--semantic-type",
        f"{input_family}-input-bundle/v1",
        "--media-type",
        "application/x-tar",
        "--compression",
        "gzip",
        "--output",
        "/qualification/receipt",
        "--idempotency-key",
        args.idempotency_key,
        "--display-name",
        f"{args.model.upper()} exact-workbench-image qualification",
        "--wait-seconds",
        "1800",
        "--poll-seconds",
        "5",
    ]
    raise SystemExit(subprocess.run(command, env=env).returncode)


if __name__ == "__main__":
    main()
