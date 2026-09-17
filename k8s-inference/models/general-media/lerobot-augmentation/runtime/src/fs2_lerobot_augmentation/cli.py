"""Command-line entry point for the controller-owned worker image."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .contracts import AugmentationRequest, ContractError
from .cosmos import CosmosError
from .dataset import DatasetError
from .worker import CancelledError, run


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Augment a localized LeRobot v3 dataset with Cosmos3-Nano")
    value.add_argument("--request", type=Path, required=True)
    value.add_argument("--operation-id", required=True)
    value.add_argument("--workspace", type=Path, required=True)
    value.add_argument(
        "--platform-base-url",
        default=os.getenv("FS2_SCIENTIFIC_INTERNAL_API_URL"),
        required=not os.getenv("FS2_SCIENTIFIC_INTERNAL_API_URL"),
    )
    value.add_argument(
        "--source-artifact",
        type=Path,
        help="Materialized dataset bundle, or exact JSON source reference for Hugging Face/object-store sources",
    )
    return value


def main() -> None:
    args = parser().parse_args()
    try:
        request = AugmentationRequest.parse(json.loads(args.request.read_text(encoding="utf-8")))
        result = run(
            request,
            operation_id=args.operation_id,
            workspace=args.workspace,
            platform_base_url=args.platform_base_url,
            source_artifact=args.source_artifact,
            workload_capability=os.getenv("FS2_SCIENTIFIC_WORKLOAD_CAPABILITY"),
        )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except ContractError as error:
        print(json.dumps({"code": "INVALID_REQUEST", "message": str(error), "retryable": False}), file=sys.stderr)
        raise SystemExit(64) from error
    except CancelledError as error:
        print(json.dumps({"code": "CANCELLED", "message": str(error), "retryable": False}), file=sys.stderr)
        raise SystemExit(130) from error
    except (DatasetError, CosmosError) as error:
        retryable = isinstance(error, CosmosError) and error.retryable
        code = error.code if isinstance(error, CosmosError) else "DATASET_INVALID"
        print(json.dumps({"code": code, "message": str(error), "retryable": retryable}), file=sys.stderr)
        raise SystemExit(75 if retryable else 65) from error


if __name__ == "__main__":
    main()
