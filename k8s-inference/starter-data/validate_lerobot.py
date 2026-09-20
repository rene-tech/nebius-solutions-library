#!/usr/bin/env python3
"""Independent full-reader check in the repository's locked LeRobot environment."""

import argparse
import hashlib
import importlib.util
import json
import tempfile
from pathlib import Path

from fs2_lerobot_augmentation.contracts import Selection
from fs2_lerobot_augmentation.dataset import extract_uploaded_bundle, open_and_validate


def main(args):
    fixtures = (
        Path(__file__).resolve().parents[1]
        / "models/general-media/lerobot-augmentation/fixtures/validate_variant.py"
    )
    spec = importlib.util.spec_from_file_location("variant_acceptance", fixtures)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = json.loads((args.run / "receipt.json").read_bytes())
    result = json.loads((args.run / "result.json").read_bytes())
    value = result.get("result", result)
    files = {a["artifact_id"]: a for a in receipt["artifacts"]}
    output_manifest = json.loads(
        (
            args.run / files[value["output_manifest"]["artifact_id"]]["local_path"]
        ).read_bytes()
    )
    bundle = next(
        e["artifact"]
        for e in output_manifest["entries"]
        if e["artifact"]["media_type"] == "application/x-tar"
    )
    source = args.pack / "physical-ai-robotics/lerobot-lighting/dataset.tar.zst"
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    selection = Selection(episodes="all", cameras="all")
    with tempfile.TemporaryDirectory(prefix="fs2-starter-lerobot-") as temporary:
        root = Path(temporary)
        extract_uploaded_bundle(source, root / "source", expected_sha256=source_sha)
        extract_uploaded_bundle(
            args.run / files[bundle["artifact_id"]]["local_path"],
            root / "variant",
            expected_sha256=bundle["sha256"],
        )
        before = open_and_validate(
            root / "source", repo_id="fs2/starter-source", selection=selection
        )
        after = open_and_validate(
            root / "variant", repo_id="fs2/starter-variant", selection=selection
        )
        provenance = json.loads(
            (root / "variant/meta/fs2-augmentation-provenance.json").read_bytes()
        )
        proof = module.compare_variant(
            before,
            after,
            replacements={(0, "observation.images.front")},
            provenance=provenance,
        )
        assert all(
            row["changed_frames"] > 0
            for row in proof["visual_comparison"]
            if row["selected"]
        )
    proof.update(
        operation_id=receipt["operation_id"],
        source_sha256=source_sha,
        output_sha256=bundle["sha256"],
    )
    (args.run / "lerobot-reader-proof.json").write_text(
        json.dumps(proof, indent=2) + "\n"
    )
    print(json.dumps(proof, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    main(parser.parse_args())
