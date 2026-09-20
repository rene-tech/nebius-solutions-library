"""Opt-in live caption/prompt probe. This does NOT qualify video generation."""
# ruff: noqa: E402

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "runtime"))
sys.path.insert(0, str(HERE.parent / "lerobot-augmentation/runtime/src"))
sys.path.insert(0, os.environ["PAIDF_MODULES"])
from fs2_video.bridge import install
from fs2_video.contracts import Recipe, recipe_identity
from fs2_video.media import digest
from fs2_video.paidf import pipeline_config, provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verify-source", action="store_true")
    args = parser.parse_args()
    install()
    from aug_utils.schema import PipelineConfig
    from captioning.factory import create_captioner

    models = provider()
    recipe = Recipe(weather="overcast")
    config = PipelineConfig.model_validate(
        pipeline_config(args.video, args.output.parent, recipe, models)
    )
    started = time.time()
    captioner = create_captioner(config, logging.getLogger("caption-probe"))
    prompt = captioner.get_caption(str(args.video))
    record = {
        "scope": "live VLM+LLM source caption and weather prompt only; no Cosmos inference or output-quality claim",
        "input_sha256": digest(args.video),
        "recipe": recipe_identity(
            recipe,
            vlm_model=models["vlm"],
            llm_model=models["llm"],
            provider_url=models["url"],
        ),
        "elapsed_seconds": time.time() - started,
        "prompt": prompt,
    }
    if args.verify_source:
        from cli import _init_evaluators

        _, verifier, _, _ = _init_evaluators(
            config.model_dump(), logging.getLogger("verification-probe")
        )
        checks = {}
        for weather in ("overcast", "clear"):
            passed, _ = verifier.verify_video_attributes(
                str(args.video),
                {"weather_condition": weather},
                {"weather_condition": ["overcast", "clear", "rain"]},
            )
            checks[weather] = bool(passed)
        record["source_weather_checks"] = checks
        record["scope"] += (
            "; live sampled-weather checks of unchanged sunny source as negative/positive controls"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps(
            {
                "caption_prompt_completed": bool(prompt),
                "elapsed_seconds": record["elapsed_seconds"],
                "receipt": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
