"""Create deterministic synthetic fixtures for visual-science qualification."""

from __future__ import annotations

import argparse
from pathlib import Path


def microscopy_fixture(path: Path, *, variant: int) -> None:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    if variant not in {0, 1}:
        raise ValueError("variant must be 0 or 1")
    image = Image.new("L", (512, 384), color=18 + variant * 4)
    draw = ImageDraw.Draw(image)
    rng = np.random.default_rng(20260919 + variant)
    count = 38 + variant * 11
    for _ in range(count):
        x = int(rng.integers(20, 492))
        y = int(rng.integers(20, 364))
        radius = int(rng.integers(5, 15))
        intensity = int(rng.integers(135, 245))
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=intensity)
    noise = rng.normal(0, 4, (384, 512))
    pixels = np.clip(np.asarray(image.filter(ImageFilter.GaussianBlur(1.3))) + noise, 0, 255).astype(np.uint8)
    Image.fromarray(pixels).save(path, format="PNG", optimize=False)


def anndata_fixture(path: Path, *, variant: int) -> None:
    import anndata as ad
    import numpy as np
    import pandas as pd

    if variant not in {0, 1}:
        raise ValueError("variant must be 0 or 1")
    rng = np.random.default_rng(20260919 + variant)
    cells, genes = 160 + 32 * variant, 96
    batch = np.array(["batch-a", "batch-b"] * (cells // 2))
    labels = np.array((["T cell", "B cell", "Unknown", "Monocyte"] * ((cells + 3) // 4))[:cells])
    means = np.full((cells, genes), 1.4, dtype=np.float32)
    means[labels == "T cell", :12] += 2.2
    means[labels == "B cell", 12:24] += 2.0
    means[labels == "Monocyte", 24:40] += 2.5
    means[batch == "batch-b", 40:55] += 0.8 + 0.2 * variant
    counts = rng.poisson(means).astype(np.float32)
    obs = pd.DataFrame(
        {"batch": pd.Categorical(batch), "cell_type": pd.Categorical(labels)},
        index=[f"synthetic-{variant}-{index:04d}" for index in range(cells)],
    )
    var = pd.DataFrame(index=[f"gene-{index:04d}" for index in range(genes)])
    ad.settings.allow_write_nullable_strings = True
    ad.AnnData(X=counts, obs=obs, var=var).write_h5ad(path, compression="gzip")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("microscopy", "anndata"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--variant", type=int, choices=(0, 1), required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.kind == "microscopy":
        microscopy_fixture(args.output, variant=args.variant)
    else:
        anndata_fixture(args.output, variant=args.variant)


if __name__ == "__main__":
    main()
