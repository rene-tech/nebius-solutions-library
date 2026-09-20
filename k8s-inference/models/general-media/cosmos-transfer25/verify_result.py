"""Evaluate a retained direct-NIM result using the pinned PAIDF checks.

This intentionally does not generate video or qualify the hosted App/workbench.
Provider credentials come from the existing protected process environment.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from fs2_video import PAIDF_REVISION
from fs2_video.bridge import install
from fs2_video.contracts import Recipe
from fs2_video.media import inspect_video, validate_alignment
from fs2_video.paidf import pipeline_config, provider


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--result-directory", type=Path, required=True)
    parser.add_argument("--weather", choices=("overcast", "clear", "rain"), default="overcast")
    args = parser.parse_args()
    destination = args.result_directory / "quality.json"
    if destination.exists():
        parser.error("quality evidence already exists; do not overwrite a retained evaluation")
    native_bytes = (args.result_directory / "receipt.json").read_bytes()
    native = json.loads(native_bytes)
    if native.get("alignment_passed") is not True:
        parser.error("the direct generation must first pass source/output alignment")
    source = inspect_video(args.source)
    output_path = args.result_directory / "output.mp4"
    generated = inspect_video(output_path)
    validate_alignment(source, generated)
    if source != native["source"] or generated != native["output"]:
        parser.error("source or result identity differs from the retained generation")
    sys.path.insert(0, os.environ["PAIDF_MODULES"])
    install()
    from aug_utils.schema import PipelineConfig
    from cli import _init_evaluators

    models = provider()
    # Reuse only the evaluator configuration. No generation/adapter call occurs.
    recipe = Recipe(weather=args.weather, max_retries=0)
    config = PipelineConfig.model_validate(pipeline_config(args.source, args.result_directory, recipe, models))
    logger = logging.getLogger("transfer25-quality")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    checker, verifier, _, _ = _init_evaluators(config.model_dump(), logger)
    receipt = {
        "schema": "fs2-serve.nebius.ai/cosmos-transfer25-direct-quality/v1",
        "paidf_revision": PAIDF_REVISION,
        "generation_receipt_sha256": hashlib.sha256(native_bytes).hexdigest(),
        "source_sha256": source["sha256"],
        "output_sha256": generated["sha256"],
        "target_weather": args.weather,
        "vlm_model": models["vlm"],
        "llm_model": models["llm"],
        "evaluation_frames": 5,
        "alignment_passed": True,
        "public_platform_path_tested": False,
        "customer_ready": False,
        "limitation": (
            "Automated motion and sampled-weather checks are not proof of physical fidelity or label validity; "
            "human review is required."
        ),
    }
    started = time.monotonic()
    stage = "motion"
    try:
        evaluated, details = checker.check_hallucination(str(args.source), str(output_path))
        receipt["motion"] = {
            key: details.get(key)
            for key in (
                "score",
                "total_frames",
                "total_hallucinated_pixels",
                "total_augmented_dynamic_pixels",
                "params",
            )
        }
        receipt["motion"].update(
            threshold=recipe.motion_threshold,
            passed=bool(
                evaluated
                and details.get("total_frames") == source["frames"]
                and details.get("total_augmented_dynamic_pixels", 0) > 0
                and details.get("score", 0) >= recipe.motion_threshold
            ),
        )
        stage = "weather"
        passed, _ = verifier.verify_video_attributes(
            str(output_path),
            {"weather_condition": args.weather},
            {"weather_condition": ["overcast", "clear", "rain"]},
        )
        receipt["weather"] = {"passed": bool(passed)}
        receipt["accepted_by_automated_checks"] = receipt["motion"]["passed"] and bool(passed)
    except Exception as error:
        receipt.update(accepted_by_automated_checks=False, failure_stage=stage, error_type=type(error).__name__)
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        destination.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)
    return 0 if receipt["accepted_by_automated_checks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
