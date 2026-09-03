"""Deterministic, in-process scientific plumbing canary.

This canary deliberately consumes no Kubernetes, accelerator, model, or customer
resource.  It exercises the packaged artifact-manifest validator and a bounded
CPU transform before scientific workers start.  It is not a model profile and
must never appear on public or admin discovery surfaces.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .profile_catalog import ScientificProfileCatalog, ScientificProfileError

CANARY_ID = "fs2-internal-scientific-cpu-v1"
_INPUT = b">fs2-internal-cpu-canary\nACDEFGHIKLMNPQRSTVWY\n"
_INPUT_SHA256 = "1e20635aeb8036a584a2b6f69da8c707b12f1f44ed452e78a472c3e0f064928e"
_SEQUENCE = "ACDEFGHIKLMNPQRSTVWY"
_OUTPUT_SHA256 = "b18338dda9fda75dbca256a7982778de61d6f0a1317ae1b0d0c2adb98ca68457"


@dataclass(frozen=True, slots=True)
class ScientificCpuCanaryReceipt:
    canary_id: str
    input_sha256: str
    output_sha256: str
    profile_count: int
    runnable_profile_count: int


def run_internal_cpu_canary(catalog: ScientificProfileCatalog) -> ScientificCpuCanaryReceipt:
    """Run the fixed startup vector and return a non-public readiness receipt."""

    input_sha256 = hashlib.sha256(_INPUT).hexdigest()
    if input_sha256 != _INPUT_SHA256:
        raise ScientificProfileError("internal scientific CPU canary input identity differs")
    catalog.validate_artifact_manifest(
        {
            "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
            "manifest_id": CANARY_ID,
            "entries": [
                {
                    "name": "input-sequence",
                    "semantic_type": "protein.sequence/v1",
                    "artifact": {
                        "artifact_id": f"{CANARY_ID}:input",
                        "sha256": input_sha256,
                        "size_bytes": len(_INPUT),
                        "media_type": "text/x-fasta",
                    },
                }
            ],
        }
    )
    if len(_SEQUENCE) != 20 or set(_SEQUENCE) != set("ACDEFGHIKLMNPQRSTVWY"):
        raise ScientificProfileError("internal scientific CPU canary semantic validation failed")
    output = json.dumps(
        {
            "alphabet": "".join(sorted(set(_SEQUENCE))),
            "length": len(_SEQUENCE),
            "sequence_sha256": hashlib.sha256(_SEQUENCE.encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    output_sha256 = hashlib.sha256(output).hexdigest()
    if output_sha256 != _OUTPUT_SHA256:
        raise ScientificProfileError("internal scientific CPU canary output identity differs")
    return ScientificCpuCanaryReceipt(
        canary_id=CANARY_ID,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        profile_count=len(catalog.list(runnable_only=False)),
        runnable_profile_count=len(catalog.list()),
    )
