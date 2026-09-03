#!/usr/bin/env python3
"""Keep the catalog's runtime recipe digests in step with the adapter sources.

``runtime_recipe_sha256`` hashes each primary adapter together with every shared
execution contract, including the localization contract and its schema. Editing
any of those changes the digest, so the value in the workload profile has to be
regenerated or it silently describes code that no longer exists.

Run with ``--check`` in CI to fail on drift, or without it to rewrite the values.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
SOLUTION_ROOT = CONTROL_PLANE_ROOT.parents[1]
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
MODEL_IDS = ("boltzgen", "proteina-complexa")

sys.path.insert(0, str(CONTROL_PLANE_ROOT / "src"))

from fs2_serve.scientific_batch.adapters.common import runtime_recipe_sha256  # noqa: E402

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift instead of rewriting")
    options = parser.parse_args(argv)

    text = PROFILE_PATH.read_text(encoding="utf-8")
    document = json.loads(text)
    profiles = {profile["model_id"]: profile for profile in document["profiles"] if "model_id" in profile}

    drifted: list[str] = []
    for model_id in MODEL_IDS:
        profile = profiles.get(model_id)
        if profile is None:
            raise SystemExit(f"{PROFILE_PATH} has no profile for {model_id}")
        recorded = profile["execution_identity"]["runtime_recipe_sha256"]
        expected = runtime_recipe_sha256(SOLUTION_ROOT, model_id)
        if _SHA256.fullmatch(recorded) is None:
            raise SystemExit(f"{model_id} runtime_recipe_sha256 is not a lowercase SHA-256")
        if recorded == expected:
            continue
        drifted.append(f"{model_id}: {recorded} -> {expected}")
        # Rewrite the literal rather than re-serializing, so refreshing a digest
        # never reformats a hand-maintained contract.
        if text.count(recorded) != 1:
            raise SystemExit(f"{model_id} recipe digest is not uniquely replaceable")
        text = text.replace(recorded, expected)

    if not drifted:
        print("scientific runtime recipes are current")
        return 0
    if options.check:
        for row in drifted:
            print(f"drift: {row}")
        print("run scripts/refresh_scientific_recipes.py to update them")
        return 1
    PROFILE_PATH.write_text(text, encoding="utf-8")
    for row in drifted:
        print(f"updated {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
