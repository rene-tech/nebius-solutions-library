#!/usr/bin/env python3
"""Validate a supplied private v2 bundle without loading weights or contacting production."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "components/control-plane/src"))

from fs2_serve.mindguard_artifacts import MindGuardV2Bundle, validate_private_bundle  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("manifest", type=Path)
parser.add_argument("artifact_root", type=Path)
args = parser.parse_args()
bundle = MindGuardV2Bundle.model_validate_json(args.manifest.read_text())
print(json.dumps(validate_private_bundle(bundle, args.artifact_root), indent=2))
