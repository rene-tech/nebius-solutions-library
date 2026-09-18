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

TERMINATION_SCHEMA = "fs2-serve.nebius.ai/lerobot-worker-error/v1"
TERMINATION_PATH = Path("/dev/termination-log")
TERMINATION_MAX_BYTES = 1024
# Static public text only: never copy exception messages, dataset paths, or
# upstream payloads/capabilities into Kubernetes termination messages.
ERROR_DETAILS = {
    "INVALID_REQUEST": "The augmentation request is invalid; check the published request schema.",
    "CANCELLED": "The augmentation was cancelled before completion; no successful dataset is claimed.",
    "DATASET_INVALID": (
        "The dataset or generated media failed validation; check dataset format, fixed-rate timing, "
        "selection and workspace limits."
    ),
    "PLATFORM_RESPONSE_INVALID": (
        "Control-plane operation or artifact response did not match the supported contract; "
        "contact the operator with the parent operation ID."
    ),
    "PLATFORM_UPSTREAM_ERROR": "The control plane rejected or could not process a delegated Cosmos request.",
    "REFERENCE_TOO_LARGE": "The selected episode reference video exceeds the supported upload bound.",
    "RUNTIME_DEPENDENCY_MISSING": "The worker is missing a required runtime dependency; contact the operator.",
    "COSMOS_MEDIA_UNREADABLE": "A Cosmos video could not be read.",
    "COSMOS_MEDIA_INVALID": "A Cosmos video is invalid or exceeds its supported size bound.",
    "COSMOS_MEDIA_ALIGNMENT_INVALID": "The generated video frame count differs from the source episode.",
    "COSMOS_MEDIA_NORMALIZATION_FAILED": "The generated video could not be restored to the source dataset geometry.",
    "COSMOS_OPERATION_FAILED": "A delegated Cosmos operation failed; inspect its child operation status.",
    "COSMOS_OPERATION_TIMEOUT": "The delegated Cosmos operation exceeded the worker wait timeout.",
    "COSMOS_OUTPUT_IDENTITY_MISMATCH": "The downloaded Cosmos artifact did not match its declared digest or size.",
    "COSMOS_OUTPUT_TOO_LARGE": "The generated Cosmos artifact exceeds the supported output bound.",
    "COSMOS_TRANSPORT_ERROR": "Communication with the control plane or artifact download failed.",
}
RETRYABLE_CODES = frozenset({"PLATFORM_UPSTREAM_ERROR", "COSMOS_OPERATION_TIMEOUT", "COSMOS_TRANSPORT_ERROR"})


def report_error(code: str, *, retryable: bool) -> None:
    if code not in ERROR_DETAILS:
        code = "COSMOS_OPERATION_FAILED"
    payload = {
        "schema": TERMINATION_SCHEMA,
        "code": code,
        "detail": ERROR_DETAILS[code],
        "retryable": retryable and code in RETRYABLE_CODES,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= TERMINATION_MAX_BYTES
    print(encoded, file=sys.stderr, flush=True)
    try:
        TERMINATION_PATH.write_text(encoded, encoding="utf-8")
    except OSError:
        # Local CLI invocations may not have a Kubernetes termination file.
        # Never replace the actual operation failure with a reporting failure.
        pass


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
        report_error("INVALID_REQUEST", retryable=False)
        raise SystemExit(64) from error
    except CancelledError as error:
        report_error("CANCELLED", retryable=False)
        raise SystemExit(130) from error
    except (DatasetError, CosmosError) as error:
        retryable = isinstance(error, CosmosError) and error.retryable and error.code in RETRYABLE_CODES
        code = error.code if isinstance(error, CosmosError) else "DATASET_INVALID"
        report_error(code, retryable=retryable)
        raise SystemExit(75 if retryable else 65) from error


if __name__ == "__main__":
    main()
