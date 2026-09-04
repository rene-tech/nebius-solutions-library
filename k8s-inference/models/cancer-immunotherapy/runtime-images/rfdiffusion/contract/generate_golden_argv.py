#!/usr/bin/env python3
"""Regenerate the golden argv for every adapter-to-image fixture.

The golden files are the machine-checkable half of the adapter handoff: they pin
exactly what this image executes for a given public request, so the adapter owner
can translate their typed public schema against a fixed target and a drift test can
fail the moment either side moves.

The argv is produced by the image's own ``build_argv``, never hand-written, so a
golden file can never disagree with the runtime by construction. Container-side
paths are substituted in so the golden matches what actually runs under the Job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import runtime_entrypoint as rt  # noqa: E402

# The paths the rendered Job mounts. Held constant so the golden argv is stable.
CONTAINER = {
    # The shared localization contract mounts the exact Base_ckpt.pt generation
    # here. The image receives this directory as --artifact-root and resolves
    # the manifest's Base_ckpt.pt path beneath it.
    "artifact_root": Path("/opt/fs2/artifacts/rfdiffusion-base-checkpoint"),
    "upstream_home": Path("/opt/rfdiffusion"),
    "scratch": Path("/tmp/fs2-rfdiffusion"),
    "output": Path("/workspace/run"),
    "python": "python",
}

FIXTURES = ("design-backbone", "scaffold-motif")


def golden_for(name: str) -> dict:
    directory = HERE / "fixtures" / name
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "input-manifest.json").read_text(encoding="utf-8"))

    parameters = rt.parse_parameters(request["parameters"])
    artifacts = rt._manifest_entries(manifest)

    checkpoint = artifacts["artifact.rfdiffusion.base-ckpt"]
    checkpoint_path = CONTAINER["artifact_root"] / checkpoint["path"]

    input_pdb = None
    if parameters.input_pdb_artifact_id:
        entry = artifacts[parameters.input_pdb_artifact_id]
        input_pdb = CONTAINER["artifact_root"] / entry["path"]

    argv = rt.build_argv(
        parameters,
        checkpoint=checkpoint_path,
        output_prefix=CONTAINER["output"] / "designs" / "design",
        hydra_run_dir=CONTAINER["scratch"] / "hydra",
        schedule_directory=CONTAINER["scratch"] / "schedules",
        input_pdb=input_pdb,
        upstream_home=CONTAINER["upstream_home"],
        python_executable=CONTAINER["python"],
    )

    return {
        "schema": "fs2.nebius.ai/rfdiffusion-adapter-to-image-golden/v1",
        "fixture": name,
        "operation": parameters.operation,
        "adapter_id": rt.ADAPTER_ID,
        "parameters_schema": rt.SCHEMA_PARAMETERS,
        "image_invocation": [
            CONTAINER["python"],
            "/opt/fs2/runtime_entrypoint.py",
            "run",
            "--request", "/var/run/fs2/request.json",
            "--input-manifest", "/var/run/fs2/input-manifest.json",
            "--artifact-root", str(CONTAINER["artifact_root"]),
            "--checkpoint-artifact-id", "artifact.rfdiffusion.base-ckpt",
            "--output", str(CONTAINER["output"]),
            "--scratch", str(CONTAINER["scratch"]),
        ],
        "upstream_argv": argv,
        "required_artifacts": [
            {
                "artifact_id": entry["artifact_id"],
                "relative_path": entry["path"],
                "sha256": entry["sha256"],
                "size_bytes": entry["size_bytes"],
                "resolved_container_path": str(CONTAINER["artifact_root"] / entry["path"]),
            }
            for entry in sorted(artifacts.values(), key=lambda e: e["artifact_id"])
        ],
        "design_indices": list(parameters.design_indices),
        "expected_markers": [
            marker
            for index in parameters.design_indices
            for marker in (
                f"designs/design_{index}.pdb",
                f"designs/design_{index}.trb",
            )
        ],
        "requested_residues": {
            "minimum": parameters.total_min_residues,
            "maximum": parameters.total_max_residues,
        },
    }


def main() -> int:
    write = "--check" not in sys.argv
    failures = []
    for name in FIXTURES:
        golden = golden_for(name)
        target = HERE / "fixtures" / name / "golden-argv.json"
        payload = json.dumps(golden, indent=2, sort_keys=True) + "\n"
        if write:
            target.write_text(payload, encoding="utf-8")
            print(f"wrote {target.relative_to(HERE.parent)}")
        else:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if current != payload:
                failures.append(name)
    if failures:
        print(f"golden argv is stale for: {', '.join(failures)}", file=sys.stderr)
        return 1
    if not write:
        print("golden argv is current for all fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
