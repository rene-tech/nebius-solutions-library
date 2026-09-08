"""Fetch immutable AltumAge artifacts and convert preprocessing metadata once.

Only checksum-verified, revision-pinned upstream pickle files are opened during
image construction. The serving process reads JSON/NumPy instead of pickle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

SOURCE_REVISION = "696c477dac9b7641bf283c48af1cc9bb0a0803a3"
SOURCE = f"https://raw.githubusercontent.com/rsinghlab/AltumAge/{SOURCE_REVISION}"
ARTIFACTS = {
    "AltumAge.pt": "f540dca1f5b2fbd7ea7cd5e43de762fe16e2b15880ed447a75bcabe13dbdc8b2",
    "multi_platform_cpgs.pkl": "068ed91ba02ec575262c7e9d0857eb9589224e478b8f2a9181c0fbcb0e26359d",
    "scaler.pkl": "68331c0b8974c192460e2ebf31e4a974da3292775f82c52d54a11dea629ff53d",
}
REFERENCE_ARTIFACT = {
    "AltumAge.h5": "2db4011115c3f877746d6dd722045da74055d25e8803368a1679d0dbaafe1846"
}


def fetch(root: Path, name: str, expected: str) -> dict[str, str | int]:
    url = f"{SOURCE}/example_dependencies/{name}"
    path = root / name
    if not path.exists():
        with urlopen(url, timeout=90) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(f"upstream artifact checksum mismatch: {name}")
        path.write_bytes(payload)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"cached artifact checksum mismatch: {name}")
    return {"url": url, "sha256": expected, "size_bytes": len(payload)}


def materialize(root: Path, *, reference: bool = False) -> dict:
    import numpy as np
    import pandas as pd
    import torch
    from pyaging.models import AltumAgeNeuralNetwork
    from torch import nn

    root.mkdir(parents=True, exist_ok=True)
    artifacts = dict(ARTIFACTS)
    if reference:
        artifacts.update(REFERENCE_ARTIFACT)
    manifest = {
        "model_id": "altumage",
        "source_revision": SOURCE_REVISION,
        "artifacts": {name: fetch(root, name, sha) for name, sha in artifacts.items()},
    }
    cpgs = [str(value) for value in pd.read_pickle(root / "multi_platform_cpgs.pkl")]
    scaler = pd.read_pickle(root / "scaler.pkl")
    if len(cpgs) != 20318 or len(set(cpgs)) != 20318:
        raise ValueError("AltumAge requires 20,318 unique ordered CpGs")
    center, scale = np.asarray(scaler.center_), np.asarray(scaler.scale_)
    if center.shape != (20318,) or scale.shape != (20318,):
        raise ValueError("AltumAge scaler shape mismatch")
    if (
        not np.isfinite(center).all()
        or not np.isfinite(scale).all()
        or (scale <= 0).any()
    ):
        raise ValueError("AltumAge scaler must be finite with positive scales")
    (root / "cpgs.json").write_text(json.dumps(cpgs) + "\n", encoding="utf-8")
    np.savez(root / "preprocessing.npz", center=center, scale=scale)
    # The official file contains an older pyaging class path. Resolve only that
    # known architecture and standard layers, then publish a plain state_dict.
    allowed = [
        (AltumAgeNeuralNetwork, "pyaging.models.neural_network_model.AltumAge"),
        nn.BatchNorm1d,
        nn.Linear,
    ]
    with torch.serialization.safe_globals(allowed):
        model = torch.load(root / "AltumAge.pt", map_location="cpu", weights_only=True)
    torch.save(model.state_dict(), root / "weights.pt")
    manifest["feature_count"] = len(cpgs)
    manifest["preprocessing"] = "official-robust-scaler"
    for name in ("cpgs.json", "preprocessing.npz", "weights.pt"):
        payload = (root / name).read_bytes()
        manifest["artifacts"][name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "derived_from": SOURCE_REVISION,
        }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--reference", action="store_true", help="also fetch original Keras weights"
    )
    args = parser.parse_args()
    print(json.dumps(materialize(args.output, reference=args.reference), indent=2))


if __name__ == "__main__":
    main()
