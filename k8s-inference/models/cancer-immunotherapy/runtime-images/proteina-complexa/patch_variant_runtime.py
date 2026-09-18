#!/usr/bin/env python3
"""Apply two fail-closed repairs to the exact pinned Complexa source tree.

The scientific parameters and checkpoint bytes remain unchanged. ListConfig is
the actual Hydra representation of multi-ligand AME targets. Failed RF3 calls
must remain failures, not synthetic zero-confidence predictions.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


SOURCE_HASHES = {
    "src/proteinfoundation/datasets/gen_dataset.py":
        "ebcc3be71c2a185046adb1c09b640a864dbafdcd0f88a6339a0861faa3f5c030",
    "src/proteinfoundation/rewards/rf3_reward.py":
        "0f2a9354266c2920ed86ca3b4289066656c58fc1fb7b334f6943b74b184861ce",
}


def transform(relative: str, source: str) -> str:
    if relative.endswith("gen_dataset.py"):
        before = "from collections.abc import Callable, Iterable\n"
        if source.count(before) != 1:
            raise ValueError("Unexpected pinned collection imports")
        source = source.replace(before, before.replace("Iterable", "Iterable, Sequence"))
        before = "elif isinstance(ligand, (list, tuple)):"
        if source.count(before) != 1:
            raise ValueError("Unexpected pinned ligand normalization")
        return source.replace(before, "elif isinstance(ligand, Sequence):")
    if relative.endswith("rf3_reward.py"):
        before = "predictions.append(self._empty_prediction())"
        if source.count(before) != 2:
            raise ValueError("Unexpected pinned RF3 error handlers")
        return source.replace(
            before,
            'raise RuntimeError("RF3 prediction failed; no valid structure returned") from e',
        )
    raise ValueError("Unrecognized pinned source file")


def patch(root: Path) -> None:
    # Validate every input before editing any file; an unexpected source tree
    # must never be accidentally presented as the reviewed patch.
    originals = {relative: (root / relative).read_bytes() for relative in SOURCE_HASHES}
    for relative, raw in originals.items():
        if hashlib.sha256(raw).hexdigest() != SOURCE_HASHES[relative]:
            raise ValueError(f"Pinned upstream source mismatch: {relative}")
    replacements = {
        relative: transform(relative, raw.decode()) for relative, raw in originals.items()
    }
    for relative, source in replacements.items():
        (root / relative).write_text(source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    patch(parser.parse_args().source)
