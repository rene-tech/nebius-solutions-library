"""Evaluate a retained native output with the unchanged NVIDIA evaluators.

This never generates, retries, repairs media, or overrides a failed verdict.
Private loopback model probes are runtime evidence, not a customer release gate.
"""

import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbench", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    sys.path.insert(0, str(args.workbench))
    import scientific_video_reference as reference
    from scientific_receipts import save

    os.environ.update(
        PAIDF_ROOT=str(args.reference), PAIDF_TRANSPORT="direct",
        PAIDF_VLM_URL="http://127.0.0.1:18250/v1",
        PAIDF_LLM_URL="http://127.0.0.1:18252/v1",
        PAIDF_VLM_MODEL=reference.REFERENCE_MODELS["vlm"],
        PAIDF_LLM_MODEL=reference.REFERENCE_MODELS["llm"],
    )
    target = args.output / "quality.json"
    if target.exists():
        raise ValueError("Refusing to overwrite a quality verdict")
    config = json.loads(args.config.read_text())
    source = Path(config["data"][0]["inputs"]["rgb"])
    generated = args.output / "output.mp4"
    source_hash, output_hash = reference.digest(source), reference.digest(generated)
    motion = reference.motion(config, source, generated)
    save(args.output / "motion.json", motion)
    attributes = reference.attributes(config, generated) if motion["passed"] else {
        "passed": False, "skipped": True, "reason": "Original NVIDIA motion gate failed"
    }
    result = {
        "nvidia_revision": reference.REVISION,
        "config_sha256": reference.CONFIG_SHA256,
        "source_sha256": source_hash, "output_sha256": output_hash,
        "motion": motion, "attributes": attributes,
        "accepted": motion["passed"] and attributes["passed"],
        "human_override": False,
        "source_unchanged": reference.digest(source) == source_hash,
        "output_unchanged": reference.digest(generated) == output_hash,
    }
    save(target, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
