"""Reuse a completed public fixture for real large-input trajectory analysis.

The TRR was format-converted from XTC; this does not recover lost coordinate
precision. No random padding or customer data is added to reach a size target.
"""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = json.loads((args.receipt / "receipt.json").read_text())
    if receipt["state"] != "verified":
        raise ValueError(
            "Use the verified completed fixture, not an incomplete transfer."
        )
    entries = json.loads((args.receipt / "output-manifest.json").read_text())["entries"]
    by_hash = {
        item["artifact"]["sha256"]: args.receipt / f"output-{index:02d}.artifact"
        for index, item in enumerate(entries)
    }
    result = next(
        json.loads(by_hash[item["artifact"]["sha256"]].read_text())
        for item in entries
        if item["semantic_type"] == "gromacs-workflow-result/v1"
    )
    selected = {
        item["path"]: item
        for item in result["files"]
        if item["path"] in {"md.trr", "md.tpr"}
    }
    if set(selected) != {"md.trr", "md.tpr"}:
        raise ValueError("The fixture must contain its topology and large trajectory.")
    args.output.mkdir(mode=0o700, exist_ok=False)
    with tarfile.open(args.output / "input.tar.gz", "w:gz") as archive:
        for name, item in selected.items():
            path = by_hash[item["sha256"]]
            with path.open("rb") as source:
                assert (
                    hashlib.file_digest(source, "sha256").hexdigest() == item["sha256"]
                )
            assert path.stat().st_size == item["size_bytes"]
            archive.add(path, arcname=name)
    request = {
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "jobs": [
            {
                "id": "trajectory-analysis",
                "steps": [
                    {"id": "check-input", "command": "check", "args": ["-f", "md.trr"]},
                    {
                        "id": "backbone-rmsd",
                        "command": "rms",
                        "args": [
                            "-s",
                            "md.tpr",
                            "-f",
                            "md.trr",
                            "-o",
                            "rmsd.xvg",
                            "-tu",
                            "ns",
                        ],
                        "stdin": "Backbone\nBackbone\n",
                        "expected_outputs": ["rmsd.xvg"],
                    },
                ],
            }
        ],
    }
    (args.output / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    summary = {
        "source_operation_id": result["operation_id"],
        "native_files": selected,
        "input_bytes": (args.output / "input.tar.gz").stat().st_size,
        "purpose": "Large real trajectory transport/analysis, not converged scientific evidence.",
    }
    assert summary["input_bytes"] > 16 * 1024**2, (
        "This fixture must exercise the live presigned input path."
    )
    (args.output / "fixture.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps({"output": str(args.output), "input_bytes": summary["input_bytes"]})
    )


if __name__ == "__main__":
    main()
