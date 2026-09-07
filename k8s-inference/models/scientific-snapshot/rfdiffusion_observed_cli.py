#!/usr/bin/env python3
"""Keep frozen RF inference/validation; add the outer supervisor's real result.

This small CLI overlay is separate from the captured source directory. Updating
observability does not mutate the checkpoint, learned weights or native proxy.
"""

import json
import os
from pathlib import Path
import re
import sys


def record_startup(output, observation):
    if (
        not isinstance(observation, dict)
        or set(observation) != {"backend", "bundle_id", "manifest_sha256"}
        or observation["backend"] not in {"normal-load", "cuda-criu"}
        or not isinstance(observation["bundle_id"], str)
        or not observation["bundle_id"]
        or not isinstance(observation["manifest_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", observation["manifest_sha256"]) is None
    ):
        raise ValueError(
            "scientific startup observation differs from supervisor contract"
        )
    result_path = output / "result.json"
    result = json.loads(result_path.read_bytes())
    if result.get("status") != "succeeded" or result.get("model_id") != "rfdiffusion":
        raise ValueError("startup metadata requires a successful original RF result")
    cache = result["cache_level"]
    if (
        cache.get("declared") != "artifact-local"
        or cache.get("gpu_snapshot_used") is not False
    ):
        raise ValueError("original RF filesystem-cache metadata differs")
    restored = observation["backend"] == "cuda-criu"
    cache.update(
        source="runtime-observed",
        gpu_snapshot_used=restored,
        observed_startup=observation,
        note="The one-shot supervisor completed CUDA+CRIU restore before native inference."
        if restored
        else "The one-shot supervisor used ordinary model loading after snapshot fallback.",
    )
    temporary = result_path.with_name(".result.startup.partial")
    with temporary.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(result_path)


def main():
    from rfdiffusion_cli_proxy import main as original_main

    code = original_main()
    observed = os.environ.get("FS2_SCIENTIFIC_STARTUP_OBSERVATION")
    if code == 0 and sys.argv[1:2] == ["run"] and observed is not None:
        output = Path(sys.argv[sys.argv.index("--output") + 1])
        record_startup(output, json.loads(observed))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
