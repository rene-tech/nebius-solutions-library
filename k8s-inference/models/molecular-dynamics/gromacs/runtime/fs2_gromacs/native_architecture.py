"""Fail a runtime build when native CUDA cubins omit a required architecture."""

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

REQUIRED_SINGLE_GPU_ARCHITECTURES = frozenset({"75", "80", "86", "89", "90"})


def validate_cubins(listing: str, required=REQUIRED_SINGLE_GPU_ARCHITECTURES):
    counts = Counter(re.findall(r"\bsm_(\d+)\.cubin\b", listing))
    missing = set(required) - counts.keys()
    if missing:
        raise ValueError("Missing native CUDA cubins: " + ", ".join(sorted(missing)))
    if len({counts[architecture] for architecture in required}) != 1:
        raise ValueError("Required architectures have different CUDA cubin counts")
    return dict(sorted(counts.items(), key=lambda item: int(item[0])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--cuobjdump", default="cuobjdump")
    parser.add_argument("--required", default=";".join(sorted(REQUIRED_SINGLE_GPU_ARCHITECTURES)))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    required = set(args.required.split(";"))
    if not required or any(not re.fullmatch(r"\d+", value) for value in required):
        parser.error("Required architectures must be semicolon-separated SM numbers")
    listing = subprocess.check_output(
        [args.cuobjdump, "--list-elf", str(args.binary)], text=True
    )
    digest = hashlib.sha256()
    with args.binary.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    result = {
        "binary": str(args.binary),
        "sha256": digest.hexdigest(),
        "required_native_architectures": sorted(required),
        "native_cubin_counts": validate_cubins(listing, required),
        "cuobjdump_listing": listing,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "cuobjdump_listing"}))


if __name__ == "__main__":
    main()
