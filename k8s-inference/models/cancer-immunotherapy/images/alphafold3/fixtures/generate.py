#!/usr/bin/env python3
"""Regenerate the controller-consumable fixtures in this directory.

Nothing here is hand-authored. The reference-data documents are produced by the
reference-data producer's own ``_terminal_receipt`` over a real (small) tree
inventoried by its own ``tree_inventory``, and the stage receipts are produced
by this runtime's own composers. A controller test that binds to these fixtures
is therefore binding to real behaviour on both sides.

The producer implementation is resolved from this repository.
``FS2_AF3_PRODUCER_MODULE`` overrides that path when generating against
another checkout.

    FS2_AF3_PRODUCER_MODULE=<checkout>/reference-data/reference_data.py \\
        python3 fixtures/generate.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parents[3]

BUNDLE = "alphafold3-public-databases-v3.0"
REVISION = "v3.0-paper-snapshot-2022-09-28"
HOST_ROOT = "/mnt/fs2-reference-data/data"
MOUNT_PATH = "/reference-data"
AUTHORIZED_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
AUTHORIZED_BYTES = 1020545840

# The reference-data plane declares the AlphaFold 3 preprocessing stage as
# 16 CPU / 64Gi / 32Gi in model-requirements.json, and refuses a smaller
# request outright. The MSA thread count is frozen to that CPU request so both
# tools use the stage they were given instead of upstream's node-derived
# min(cpu_count, 8).
CANONICAL_CPU = 16
CANONICAL_MSA_THREADS = 16

PLACEMENT = {
    "resource_class": "cpu",
    "node_selector": {"fs2.nebius.ai/pool": "reference-data-cpu"},
    "tolerations": [
        {
            "key": "dedicated",
            "operator": "Equal",
            "value": "fs2-reference-data",
            "effect": "NoSchedule",
        }
    ],
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _producer():
    candidates = []
    override = os.environ.get("FS2_AF3_PRODUCER_MODULE")
    if override:
        candidates.append(Path(override))
    candidates.append(REPO / "reference-data" / "reference_data.py")
    for candidate in candidates:
        if candidate.is_file():
            module = _load("reference_data_producer", candidate)
            if hasattr(module, "_terminal_receipt"):
                return module
    raise SystemExit(
        "the reference-data producer does not expose _terminal_receipt; set "
        "FS2_AF3_PRODUCER_MODULE to a checkout that does"
    )


def write(name: str, document: dict) -> None:
    (HERE / name).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    print(f"wrote fixtures/{name}")


def main() -> int:
    af3 = _load("af3_runtime", ROOT / "runtime" / "af3_runtime.py")
    producer = _producer()

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        staging = root / "staging"
        (staging / "mmcif_files").mkdir(parents=True)
        (staging / "pdb_seqres_2022_09_28.fasta").write_text(">1abc_A\nMKV\n", "utf-8")
        (staging / "rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta").write_text(
            ">RF00001\nGGA\n", "utf-8"
        )
        (staging / "mmcif_files" / "1abc.cif").write_text("data_1ABC\n", "utf-8")

        files, tree_sha256, expanded_bytes = producer.tree_inventory(staging)
        inventory = {
            "schema": producer.INVENTORY_SCHEMA,
            "bundle_id": BUNDLE,
            "revision": REVISION,
            "tree_sha256": tree_sha256,
            "expanded_bytes": expanded_bytes,
            "file_count": len(files),
            "files": files,
        }
        inventory_sha256 = producer.sha256_bytes(producer.canonical_json(inventory))
        manifest = {
            "schema": producer.MANIFEST_SCHEMA,
            "bundle_id": BUNDLE,
            "revision": REVISION,
            "source_catalog_sha256": "b" * 64,
            "access_receipt_sha256": None,
            "created_at": "2026-09-03T00:00:00Z",
            "content": {
                "tree_sha256": tree_sha256,
                "expanded_bytes": expanded_bytes,
                "file_count": len(files),
                "inventory_sha256": inventory_sha256,
                "files": files,
            },
        }
        manifest_sha256 = producer.sha256_bytes(producer.canonical_json(manifest))

        receipt = producer._terminal_receipt(
            {"id": BUNDLE, "revision": REVISION},
            manifest,
            manifest_sha256,
            host_root=HOST_ROOT,
            placement=PLACEMENT,
        )

        sub_path = receipt["storage"]["dataset_sub_path"]
        tree = root / sub_path
        tree.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(tree)
        (tree / ".fs2-manifest-sha256").write_text(manifest_sha256, "utf-8")
        manifest_path = root / "manifests" / "sha256" / f"{manifest_sha256}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest), "utf-8")

        binding = af3.bind_reference_tree(receipt, mount_root=root)
        manifest_uri = f"file://{MOUNT_PATH}/manifests/sha256/{manifest_sha256}.json"

        write("reference-terminal-receipt.json", receipt)
        write("reference-published-manifest.json", manifest)
        write("preprocess-reference-data.json", binding.preprocess_reference_data(manifest_uri))

        # Restate the temporary mount at its production location.
        reference = binding.as_receipt()
        reference["database_root"] = f"{MOUNT_PATH}/{sub_path}"
        reference["manifest_path"] = f"{MOUNT_PATH}/manifests/sha256/{manifest_sha256}.json"

        cache = af3.CacheReport(
            root="/cache/alphafold3",
            jax_dir="/cache/alphafold3/jax",
            triton_dir="/cache/alphafold3/triton",
            xdg_dir="/cache/alphafold3/xdg",
            writable=True,
        )
        data_plan = af3.compose_data_argv(
            json_path=Path("/input/fold_input.json"),
            output_dir=Path("/output"),
            database_root=Path(f"{MOUNT_PATH}/{sub_path}"),
            cache=cache,
            threads=CANONICAL_MSA_THREADS,
            cpu_request=CANONICAL_CPU,
        )
        write(
            "data-stage-receipt.json",
            {
                "schema": af3.RECEIPT_SCHEMA,
                "mode": "data",
                "status": "PLANNED",
                "reference_data": reference,
                "cache": cache.as_receipt(),
                "plan": data_plan.as_receipt(),
                "cpu_envelope": {
                    "msa_threads": CANONICAL_MSA_THREADS,
                    "cpu_request": CANONICAL_CPU,
                    "jackhmmer_n_cpu": CANONICAL_MSA_THREADS,
                    "nhmmer_n_cpu": CANONICAL_MSA_THREADS,
                    "upstream_default_overridden": True,
                    "upstream_default": (
                        "min(cpu_count, 8), derived from the node rather than the pod"
                    ),
                },
            },
        )

        inference_plan = af3.compose_inference_argv(
            json_path=Path("/handoff/fold_input_data.json"),
            output_dir=Path("/output"),
            model_dir=Path("/models"),
            cache=cache,
        )
        write(
            "inference-stage-receipt.json",
            {
                "schema": af3.RECEIPT_SCHEMA,
                "mode": "inference",
                "status": "PLANNED",
                "parameters": {
                    "artifact_id": "alphafold3-parameters",
                    "path": "/models/af3.bin.zst",
                    "size_bytes": AUTHORIZED_BYTES,
                    "sha256": AUTHORIZED_SHA256,
                    "identity_kind": "file-digest",
                    "read_only_mount": True,
                    "deep_verified": False,
                },
                "model_dir": {"path": "/models", "candidates": ["af3.bin.zst"]},
                "cache": cache.as_receipt(),
                "plan": inference_plan.as_receipt(),
            },
        )

        write(
            "failure-receipt.json",
            {
                "schema": af3.RECEIPT_SCHEMA,
                "mode": "inference",
                "status": "FAIL",
                "error": (
                    "parameter object size mismatch at /models/af3.bin.zst: found 33 bytes, "
                    f"authorized artifact is {AUTHORIZED_BYTES} bytes"
                ),
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
