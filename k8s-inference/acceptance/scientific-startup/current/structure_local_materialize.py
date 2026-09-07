"""Feed already authorized exact input bytes into the unchanged materializer."""

import hashlib
from pathlib import Path

from fs2_serve.scientific_batch.companion import WorkloadArtifactHttpClient
from fs2_serve.scientific_companion_cli import main


def download(
    self, artifact_id, *, expected_digest, expected_size_bytes, expected_media_type
):
    payload = (Path("/benchmark-inputs") / str(artifact_id)).read_bytes()
    if (
        len(payload) != expected_size_bytes
        or "sha256:" + hashlib.sha256(payload).hexdigest() != expected_digest
    ):
        raise ValueError("benchmark input differs from exact original frozen pointer")
    return payload


WorkloadArtifactHttpClient.download = download
main()
