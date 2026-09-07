"""Feed exact, customer-authorized bytes to the unchanged materializer locally."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fs2_serve.scientific_batch.companion import WorkloadArtifactHttpClient
from fs2_serve.scientific_companion_cli import main


def download(
    self: WorkloadArtifactHttpClient,
    artifact_id: str,
    *,
    expected_digest: str,
    expected_size_bytes: int,
    expected_media_type: str,
) -> bytes:
    del self, expected_media_type
    payload = (Path("/benchmark-inputs") / str(artifact_id)).read_bytes()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if len(payload) != expected_size_bytes or digest != expected_digest:
        raise ValueError("benchmark input differs from its frozen pointer")
    return payload


WorkloadArtifactHttpClient.download = download
main()
