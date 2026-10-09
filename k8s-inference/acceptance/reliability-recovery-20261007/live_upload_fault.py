"""Run inside a QA collector only: lose an accepted finalize response, then retry.

The real API and object store perform the writes. Only the first completed
finalize response is discarded locally. No proxy, global setting or other job
is modified. Uploaded test bytes remain attributable to this internal attempt.
"""

import json
import os
import tempfile
from pathlib import Path

import httpx
from fs2_serve.scientific_batch.companion import WorkloadArtifactHttpClient


class LoseAcceptedFinalize(httpx.BaseTransport):
    def __init__(self):
        self.upstream = httpx.HTTPTransport()
        self.finalizes = []

    def handle_request(self, request):
        response = self.upstream.handle_request(request)
        if request.url.path.endswith("uploads:finalize"):
            self.finalizes.append(request.content)
            response.read()
            if response.status_code == 200 and len(self.finalizes) == 1:
                response.close()
                raise httpx.RemoteProtocolError(
                    "QA injected lost accepted finalize response", request=request
                )
        return response

    def close(self):
        self.upstream.close()


transport = LoseAcceptedFinalize()
with tempfile.TemporaryDirectory(
    prefix="fs2-checkpoint-transport-qa-", dir="/mnt/fs2-scientific"
) as directory:
    path = Path(directory) / "closed-output.bin"
    path.write_bytes(b"internal QA immutable checkpoint transfer probe 20261007")
    with httpx.Client(transport=transport, timeout=30) as http:
        client = WorkloadArtifactHttpClient(
            base_url=os.environ["FS2_SCIENTIFIC_INTERNAL_API_URL"],
            fallback_base_url=os.environ.get(
                "FS2_SCIENTIFIC_INTERNAL_FALLBACK_API_URL"
            ),
            capability=os.environ["FS2_SCIENTIFIC_WORKLOAD_CAPABILITY"],
            client=http,
        )
        result = client.upload_files(
            identity="fs2-reliability-fault-" + os.environ["FS2_ATTEMPT_ID"],
            paths=(path,),
            media_type="application/octet-stream",
            compression=None,
        )
    assert len(result) == 1 and len(transport.finalizes) == 2
    assert transport.finalizes[0] == transport.finalizes[1]
    print(
        json.dumps(
            {
                "test": "accepted-finalize-response-loss",
                "passed": True,
                "attempt_id": os.environ["FS2_ATTEMPT_ID"],
                "finalize_attempts": 2,
                "artifact_id": result[0]["artifact_id"],
            }
        )
    )
