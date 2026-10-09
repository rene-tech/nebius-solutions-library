"""One-million-real-cell capacity test, not a biological accuracy benchmark.

Use the current CELLxGENE HLCA full release, retain only raw counts and metadata
for the first 1M cells. This contiguous subset avoids inventing duplicated cells;
it is not a statistically representative or held-out scientific cohort.
"""

import json
import shutil
import time
import urllib.request
import sys
from pathlib import Path

import anndata as ad
import h5py
import scipy.sparse as sp

from fs2_gromacs.files import atomic_json, digest_file
from fs2_scvi import PARAMETER_SCHEMA
from fs2_scvi.worker import Workflow

SOURCE = "https://datasets.cellxgene.cziscience.com/5f863718-02a6-46cb-83e7-52be988b915b.h5ad"
ROOT = Path("/work/atlas-r5")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    # Re-qualify full-state restore and mapping in this exact runtime image.
    sys.path.insert(0, "/opt/fs2")
    from qualify_runtime import run_components
    if not (ROOT / "components-r6/component-receipt.json").exists():
        run_components(ROOT / "components-r6")
    source = ROOT / "hlca-full.h5ad"
    started = time.monotonic()
    if not source.exists():
        partial = source.with_suffix(".download")
        with (
            urllib.request.urlopen(SOURCE, timeout=120) as response,
            partial.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle, length=8 * 1024**2)
        partial.rename(source)
    if source.stat().st_size != 21860171922:
        raise ValueError("Full HLCA source release changed")
    workspace = ROOT / "workspace"
    workspace.mkdir(exist_ok=True)
    selected = workspace / "input.h5ad"
    if not selected.exists():
        with h5py.File(source) as handle:
            obs = ad.io.read_elem(handle["obs"]).iloc[:1_000_000].copy()
            var = ad.io.read_elem(handle["raw/var"])
            # Avoid AnnData's backed slice depending on private SciPy matrix
            # internals. H5AD CSR arrays have a stable public encoding.
            matrix = handle["raw/X"]
            if matrix.attrs["encoding-type"] != "csr_matrix":
                raise ValueError("This cohort expects CSR raw counts")
            indptr = matrix["indptr"][:1_000_001]
            stop = int(indptr[-1])
            counts = sp.csr_matrix(
                (matrix["data"][:stop], matrix["indices"][:stop], indptr),
                shape=(1_000_000, len(var)),
            )
        ad.settings.allow_write_nullable_strings = True
        ad.AnnData(counts, obs=obs, var=var).write_h5ad(selected)
        del counts, obs, var
    with h5py.File(selected) as handle:
        obs = ad.io.read_elem(handle["obs"])
    batch = next(
        (key for key in ("dataset", "dataset_name", "donor_id") if key in obs), None
    )
    if batch is None or "cell_type" not in obs:
        raise ValueError("HLCA full metadata lacks the required batch/label columns")
    atomic_json(
        ROOT / "dataset.json",
        {
            "source": SOURCE,
            "source_sha256": digest_file(source),
            "source_size_bytes": source.stat().st_size,
            "input_sha256": digest_file(selected),
            "input_size_bytes": selected.stat().st_size,
            "cells": len(obs),
            "selection": "first 1,000,000 real cells",
            "license": "CC BY 4.0",
            "citation": "Sikkema et al. 2023, doi:10.1038/s41591-023-02327-2",
            "preparation_seconds": time.monotonic() - started,
        },
    )
    print(json.dumps({"event": "atlas_prepared", "cells": len(obs)}), flush=True)
    parameters = {
        "schema": PARAMETER_SCHEMA,
        "resource_profile": "atlas",
        "method": "scanvi",
        "counts_source": "X",
        "batch_key": batch,
        "labels_key": "cell_type",
        "n_top_genes": 2000,
        "hvg_span": 1.0,
        "batch_size": 512,
        "visualization": "sample",
        "output_destination": "platform-artifacts",
    }
    result = Workflow(parameters, workspace, "hlca-1m-runtime-qualification").run()
    atomic_json(ROOT / "receipt.json", result)
    print(
        json.dumps(
            {
                "event": "atlas_completed",
                "cells": result["cells"],
                "timings": result["timings_seconds"],
                "public_customer_path_qualified": False,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
