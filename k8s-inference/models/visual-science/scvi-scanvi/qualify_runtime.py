"""Real-GPU component qualification; this does NOT qualify the public API/MCP.

Start with interruption/annotation/reference tests, then real HLCA training.
Retain raw stage logs and result inventories; never call this clinical validation.
"""

import argparse
import json
import shutil
import time
import urllib.request
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
import pandas as pd
from scipy import sparse

from fs2_scvi import PARAMETER_SCHEMA
from fs2_scvi.worker import Workflow
from fs2_gromacs.files import atomic_json, digest_file

CORE = "https://datasets.cellxgene.cziscience.com/688185ad-11c2-4172-a53a-f4f1f4076860.h5ad"


def fixture(path):
    rng = np.random.default_rng(1729)
    counts = rng.poisson(2, (512, 128)).astype(np.float32)
    counts[:256, :32] += 5
    counts[256:, 32:64] += 5
    obs = pd.DataFrame(
        {"batch": ["a", "b"] * 256, "cell_type": ["T"] * 256 + ["B"] * 256},
        index=[f"synthetic-{n}" for n in range(512)],
    )
    ad.settings.allow_write_nullable_strings = True
    ad.AnnData(sparse.csr_matrix(counts), obs=obs).write_h5ad(path)


def run_components(root):
    workspace = root / "restart-component"
    workspace.mkdir(parents=True)
    fixture(workspace / "input.h5ad")
    params = {
        "schema": PARAMETER_SCHEMA,
        "method": "scanvi",
        "batch_key": "batch",
        "labels_key": "cell_type",
        "max_epochs": 3,
        "scanvi_max_epochs": 2,
        "checkpoint_every_n_epochs": 1,
        "early_stopping": False,
        "gene_selection": "all",
        "visualization": "none",
        "output_destination": "platform-artifacts",
    }

    class InterruptAfterCommit(Workflow):
        def publish(self):
            super().publish()
            if self.state["generation"] == 1:
                assert self.state["epoch"] == 0, (
                    "must interrupt mid-training, not after completion"
                )
                raise InterruptedError(
                    "qualification: simulate worker loss after committed epoch checkpoint"
                )

    interrupted = False
    try:
        InterruptAfterCommit(params, workspace, "runtime-qualification-restart").run()
    except InterruptedError:
        interrupted = True
    assert interrupted, "fault injection never ran"
    resumed = Workflow(params, workspace, "runtime-qualification-restart").run()
    assert resumed["completed_stages"] == ["scvi", "scanvi"]
    predictions = pd.read_csv(workspace / "data/predicted_labels.csv")
    assert len(predictions) == 512
    query = root / "query-component"
    query.mkdir()
    fixture(query / "input.h5ad")
    shutil.copyfile(workspace / "data/reference.tar.gz", query / "reference.tar.gz")
    mapped = Workflow(
        {**params, "mode": "map-query", "query_max_epochs": 2},
        query,
        "runtime-qualification-query",
    ).run()
    assert mapped["cells"] == 512 and mapped["reference_sha256"]
    atomic_json(
        root / "component-receipt.json",
        {
            "passed": True,
            "scope": "GPU runtime only; synthetic scientific fixture",
            "restart": resumed,
            "reference_mapping": mapped,
        },
    )
    print(
        json.dumps(
            {"event": "components_passed", "public_customer_path_qualified": False}
        ),
        flush=True,
    )


def run_core(root, *, maximum_epochs=None):
    source = root / "hlca-core.h5ad"
    started = time.monotonic()
    if not source.exists():
        with (
            urllib.request.urlopen(CORE, timeout=120) as response,
            source.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle, length=8 * 1024**2)
    if source.stat().st_size != 5873612847:
        raise ValueError(
            "HLCA asset differs from the published 6 October 2026 CELLxGENE file size"
        )
    with h5py.File(source, "r") as handle:
        obs = ad.io.read_elem(handle["obs"])
        source_name = "raw.X" if "raw/X" in handle else "X"
    batch_key = next(
        (key for key in ("dataset", "dataset_name", "donor_id") if key in obs), None
    )
    if not batch_key or "cell_type" not in obs:
        raise ValueError(
            f"HLCA batch/label metadata missing; available columns: {list(obs.columns)}"
        )
    atomic_json(
        root / "dataset.json",
        {
            "source": CORE,
            "dataset_id": "066943a2-fdac-4b29-b348-40cede398e4e",
            "cells": len(obs),
            "sha256": digest_file(source),
            "size_bytes": source.stat().st_size,
            "download_seconds": time.monotonic() - started,
            "counts_source": source_name,
            "batch_key": batch_key,
            "license": "CC BY 4.0",
            "citation": "Sikkema et al. 2023, Nature Medicine, doi:10.1038/s41591-023-02327-2",
        },
    )
    workspace = root / "hlca-core"
    workspace.mkdir(exist_ok=True)
    (workspace / "input.h5ad").hardlink_to(source)
    parameters = {
        "schema": PARAMETER_SCHEMA,
        "method": "scanvi",
        "counts_source": source_name,
        "batch_key": batch_key,
        "labels_key": "cell_type",
        "n_top_genes": 2000,
        "max_epochs": maximum_epochs,
        "scanvi_max_epochs": 20,
        "batch_size": 512,
        "visualization": "sample",
        "visualization_cells": 20000,
        "output_destination": "platform-artifacts",
    }
    result = Workflow(parameters, workspace, "hlca-core-runtime-qualification").run()
    atomic_json(root / "hlca-receipt.json", result)
    print(
        json.dumps(
            {
                "event": "hlca_completed",
                "cells": result["cells"],
                "timings": result["timings_seconds"],
                "public_customer_path_qualified": False,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--components-only", action="store_true")
    parser.add_argument("--max-epochs", type=int)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    run_components(args.root)
    if not args.components_only:
        run_core(args.root, maximum_epochs=args.max_epochs)
