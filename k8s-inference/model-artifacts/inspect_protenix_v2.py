#!/usr/bin/env python3
"""Safely compare the Protenix v2 mirror checkpoint to the exact v2 source.

This verifier never executes checkpoint pickle globals: PyTorch loading is
forced to ``weights_only=True``, CPU placement, and mmap. It constructs the
``protenix-v2`` architecture from an exact source checkout, then compares every
state key and tensor shape without copying checkpoint tensors into the model.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types
from typing import Any, Mapping


SOURCE_REVISION = "2475421477ab414b571149ad4a875c390ff8a35d"
MIRROR_REPOSITORY = "TMF001/protenix-v2-weights"
MIRROR_REVISION = "653edab28103133512575365130916e3fd23ecc3"
EXPECTED_BYTES = 1859785497
EXPECTED_SHA256 = "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599"
EXPECTED_MD5 = "49016ebf4775bf6b629bc4dc77b6673e"
EXPECTED_KEYS = 4174
EXPECTED_PARAMETERS = 464442431
EXPECTED_TENSOR_DTYPE_COUNTS = {"torch.float32": EXPECTED_KEYS}
INSPECTION_IMAGE_REFERENCE = (
    "redacted-private-registry/protenix-v2@sha256:"
    "ad2a55f1740f49296ec730e9ff4f1d06ad391a87354f03b2921f960fe0f6d240"
)
INSPECTION_IMAGE_DIGEST = "sha256:ad2a55f1740f49296ec730e9ff4f1d06ad391a87354f03b2921f960fe0f6d240"
INSPECTION_IMAGE_TORCH_VERSION = "2.7.1+cu126"
OFFICIAL_URI = "https://protenix.tos-cn-beijing.volces.com/checkpoint/protenix-v2.pt"
MIRROR_URL = (
    "https://huggingface.co/TMF001/protenix-v2-weights/resolve/"
    f"{MIRROR_REVISION}/protenix-v2.pt"
)
BUFFER_BYTES = 4 * 1024 * 1024


def hash_file(path: Path) -> tuple[int, str, str]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BUFFER_BYTES), b""):
            size += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
    return size, sha256.hexdigest(), md5.hexdigest()


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8") + b"\n"
    return hashlib.sha256(encoded).hexdigest()


def source_revision(source_tree: Path) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={source_tree}", "-C", str(source_tree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def source_model(source_tree: Path) -> tuple[Any, Any]:
    sys.path.insert(0, str(source_tree))
    # Importing the source otherwise invokes CUDA extension compilation even
    # though this verification performs no forward pass. The extension is only
    # called from forward methods; an empty import stub leaves architecture
    # construction and state_dict enumeration unchanged.
    sys.modules.setdefault("fast_layer_norm_cuda_v2", types.ModuleType("fast_layer_norm_cuda_v2"))

    from configs.configs_base import configs as configs_base  # noqa: PLC0415
    from configs.configs_data import data_configs  # noqa: PLC0415
    from configs.configs_inference import inference_configs  # noqa: PLC0415
    from configs.configs_model_type import model_configs  # noqa: PLC0415
    from protenix.config.config import parse_configs  # noqa: PLC0415
    from protenix.model.protenix import Protenix  # noqa: PLC0415

    def deep_update(destination: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
        for key, value in update.items():
            if isinstance(value, Mapping) and isinstance(destination.get(key), Mapping):
                deep_update(destination[key], value)
            else:
                destination[key] = value
        return destination

    base = copy.deepcopy({**configs_base, **{"data": data_configs}, **inference_configs})
    deep_update(base, model_configs["protenix-v2"])
    config = parse_configs(
        configs=base,
        arg_str=(
            "--model_name protenix-v2 "
            "--triangle_attention torch --triangle_multiplicative torch"
        ),
        fill_required_with_null=True,
    )
    return Protenix(config), config


def inspect(checkpoint_path: Path, source_tree: Path) -> dict[str, Any]:
    revision = source_revision(source_tree)
    if revision != SOURCE_REVISION:
        raise RuntimeError(f"source revision mismatch: {revision}")
    size, sha256, md5 = hash_file(checkpoint_path)
    if (size, sha256, md5) != (EXPECTED_BYTES, EXPECTED_SHA256, EXPECTED_MD5):
        raise RuntimeError(
            "checkpoint byte identity mismatch: "
            f"bytes={size}, sha256={sha256}, md5={md5}"
        )

    import torch  # noqa: PLC0415

    checkpoint = torch.load(
        checkpoint_path,
        map_location="cpu",
        mmap=True,
        weights_only=True,
    )
    if type(checkpoint) is not dict or set(checkpoint) != {"model"}:
        raise RuntimeError("checkpoint does not contain the required top-level model mapping")
    raw_state = checkpoint["model"]
    if not isinstance(raw_state, Mapping) or not raw_state:
        raise RuntimeError("checkpoint model entry is not a non-empty state mapping")
    first_key = next(iter(raw_state))
    prefix_stripped = isinstance(first_key, str) and first_key.startswith("module.")
    checkpoint_state = {
        (key.removeprefix("module.") if prefix_stripped else key): value
        for key, value in raw_state.items()
    }
    if not all(isinstance(key, str) and isinstance(value, torch.Tensor) for key, value in checkpoint_state.items()):
        raise RuntimeError("checkpoint model mapping contains a non-tensor state entry")

    model, config = source_model(source_tree)
    expected_state = model.state_dict()
    checkpoint_keys = set(checkpoint_state)
    source_keys = set(expected_state)
    missing = sorted(source_keys - checkpoint_keys)
    unexpected = sorted(checkpoint_keys - source_keys)
    common = sorted(source_keys & checkpoint_keys)
    shape_mismatches = [
        {
            "key": key,
            "checkpoint": list(checkpoint_state[key].shape),
            "source": list(expected_state[key].shape),
        }
        for key in common
        if tuple(checkpoint_state[key].shape) != tuple(expected_state[key].shape)
    ]
    checkpoint_inventory = [
        {"key": key, "shape": list(checkpoint_state[key].shape)}
        for key in sorted(checkpoint_state)
    ]
    source_inventory = [
        {"key": key, "shape": list(expected_state[key].shape)}
        for key in sorted(expected_state)
    ]
    checkpoint_inventory_digest = canonical_digest(checkpoint_inventory)
    source_inventory_digest = canonical_digest(source_inventory)
    checkpoint_parameters = sum(value.numel() for value in checkpoint_state.values())
    checkpoint_dtype_counts = dict(
        sorted(Counter(str(value.dtype) for value in checkpoint_state.values()).items())
    )
    source_parameters = sum(parameter.numel() for parameter in model.parameters())
    strict_match = not missing and not unexpected and not shape_mismatches
    if (
        len(checkpoint_state) != EXPECTED_KEYS
        or len(expected_state) != EXPECTED_KEYS
        or checkpoint_parameters != EXPECTED_PARAMETERS
        or checkpoint_dtype_counts != EXPECTED_TENSOR_DTYPE_COUNTS
        or source_parameters != EXPECTED_PARAMETERS
        or checkpoint_inventory_digest != source_inventory_digest
        or not strict_match
    ):
        raise RuntimeError("checkpoint state does not exactly match the Protenix v2 source architecture")

    return {
        "schema": "fs2-serve.nebius.ai/third-party-model-mirror-verification/v1",
        "artifact_id": "protenix-v2",
        "observed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "conclusion": "mirror-verified-not-publisher-byte-compared",
        "canonical_source": {
            "publisher": "ByteDance",
            "official_uri": OFFICIAL_URI,
            "source_repository": "https://github.com/bytedance/Protenix",
            "source_revision": revision,
            "model_name": "protenix-v2",
            "official_uri_observation": "HTTP 403 from eu-north1 operator region",
            "publisher_digest_available": False,
            "publisher_byte_compared": False,
        },
        "mirror": {
            "relationship": "third-party-mirror",
            "repository": MIRROR_REPOSITORY,
            "repository_revision": MIRROR_REVISION,
            "url": MIRROR_URL,
            "lfs_oid_sha256": EXPECTED_SHA256,
            "lfs_size": EXPECTED_BYTES,
        },
        "byte_verification": {
            "bytes": size,
            "sha256": sha256,
            "md5": md5,
            "expected_md5": EXPECTED_MD5,
            "all_expected_values_match": True,
        },
        "checkpoint_inspection": {
            "torch_version": torch.__version__,
            "load_mode": "weights-only-mmap-cpu",
            "root_type": type(checkpoint).__name__,
            "top_level_keys": sorted(str(key) for key in checkpoint),
            "state_key": "model",
            "state_type": type(raw_state).__name__,
            "ddp_module_prefix_stripped": prefix_stripped,
            "state_key_count": len(checkpoint_state),
            "tensor_count": len(checkpoint_state),
            "tensor_dtype_counts": checkpoint_dtype_counts,
            "parameter_count": checkpoint_parameters,
            "element_count": checkpoint_parameters,
            "first_key": min(checkpoint_state),
            "last_key": max(checkpoint_state),
            "key_shape_inventory_sha256": checkpoint_inventory_digest,
        },
        "pinned_runtime_image_inspection": {
            "observation_source": "manager-finalized-plus-operator-network-none-recheck",
            "image_reference": INSPECTION_IMAGE_REFERENCE,
            "image_digest": INSPECTION_IMAGE_DIGEST,
            "image_qualification_state": "unqualified-inspection-only",
            "network_mode": "none",
            "torch_version": INSPECTION_IMAGE_TORCH_VERSION,
            "load_mode": "weights-only-mmap-cpu",
            "root_type": "dict",
            "top_level_keys": ["model"],
            "state_type": "OrderedDict",
            "tensor_count": EXPECTED_KEYS,
            "tensor_dtype_counts": EXPECTED_TENSOR_DTYPE_COUNTS,
            "element_count": EXPECTED_PARAMETERS,
        },
        "source_architecture": {
            "state_key_count": len(expected_state),
            "parameter_count": source_parameters,
            "c_z": config.c_z,
            "pairformer_c_z": config.model.pairformer.c_z,
            "relative_position_c_z": config.model.relative_position_encoding.c_z,
            "key_shape_inventory_sha256": source_inventory_digest,
        },
        "comparison": {
            "missing_key_count": len(missing),
            "unexpected_key_count": len(unexpected),
            "shape_mismatch_count": len(shape_mismatches),
            "strict_key_shape_match": strict_match,
        },
        "limitations": [
            "The publisher object was inaccessible and no publisher digest was available, so publisher byte equivalence was not tested.",
            "The pinned image digest is an offline inspection environment only and is explicitly not deployable or H100-qualified.",
            "This is an offline checkpoint architecture inspection, not an H100 semantic inference qualification.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--source-tree", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect(args.checkpoint.resolve(), args.source_tree.resolve())
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
