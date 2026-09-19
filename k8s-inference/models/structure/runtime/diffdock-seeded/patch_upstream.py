"""Propagate the request seed through pinned DiffDock's RDKit preprocessing.

No weights or sampling limits change. Verify original source bytes before
applying this small, reviewable upstream API extension during image build.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


ORIGINAL_SHA256 = {
    "datasets/process_mols.py": "6918a399ea25fd84466b90ce74656b4e35c1e6106d0e18588f789686ffa289af",
    "utils/inference_utils.py": "80a075bdb3bfb01ab6bcfe1b93607317d0b9d130393fb25fdc86bcce8e475f67",
}


def replace_exact(text: str, old: str, new: str, count: int = 1) -> str:
    if text.count(old) != count:
        raise ValueError("Pinned DiffDock source pattern changed")
    return text.replace(old, new)


def transform(path: str, text: str) -> str:
    if path == "datasets/process_mols.py":
        text = replace_exact(text, "def generate_conformer(mol):", "def generate_conformer(mol, random_seed=None):")
        text = replace_exact(text, "    while failures < 3 and id == -1:\n", "    while failures < 3 and id == -1:\n"
            "        if random_seed is not None:\n"
            "            ps.randomSeed = (random_seed + failures - 1) % 2147483647 + 1\n")
        text = replace_exact(text, "        ps.useRandomCoords = True\n", "        ps.useRandomCoords = True\n"
            "        if random_seed is not None:\n"
            "            ps.randomSeed = (random_seed + failures - 1) % 2147483647 + 1\n")
    elif path == "utils/inference_utils.py":
        text = replace_exact(text, "atom_max_neighbors=None, knn_only_graph=False):", "atom_max_neighbors=None, knn_only_graph=False, random_seed=None):")
        text = replace_exact(text, "        self.knn_only_graph = knn_only_graph\n", "        self.knn_only_graph = knn_only_graph\n        self.random_seed = random_seed\n")
        text = replace_exact(text, "                generate_conformer(mol)\n", "                generate_conformer(mol, random_seed=self.random_seed)\n", count=2)
    else:
        raise ValueError("Unexpected upstream file")
    compile(text, path, "exec")
    return text


def apply(root: Path) -> None:
    pending = {}
    for relative, expected in ORIGINAL_SHA256.items():
        path = root / relative
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Unexpected original DiffDock source digest: {relative}")
        pending[path] = transform(relative, raw.decode())
    for path, text in pending.items():
        path.write_text(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    apply(parser.parse_args().upstream)
