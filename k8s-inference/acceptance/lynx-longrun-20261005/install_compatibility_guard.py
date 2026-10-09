"""Build-time-only admission guard for the reader-first transition image.

Legacy replicas cannot read compressed immutable state. New compatibility
readers must read and reserialize it, but must not originate it until every
legacy replica has exited. This image keeps the old seven-day API/worker
contract. The final image contains neither this installer nor this guard.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

LEGACY_SERVICE_SHA256 = "deea3651d384453d3b0041c3caa16b5843fd2b99a0cee6849c719e7d6e402f39"
ANCHOR = "            # Validate with the actual durable reader before the enclosing\n"
GUARD = '''            if any(
                isinstance(payload.get(name), dict)
                and payload[name].get("encoding") == "fs2-serve.nebius.ai/scientific-immutable-metadata/zstd-v1"
                for name in ("input_manifest", "adapter_execution")
            ):
                raise ScientificProfileError("large continuation metadata is unavailable during compatibility rollout")
'''


def guarded_source(raw: bytes) -> bytes:
    if hashlib.sha256(raw).hexdigest() != LEGACY_SERVICE_SHA256:
        raise ValueError("compatibility image does not contain the expected legacy admission implementation")
    source = raw.decode()
    if source.count(ANCHOR) != 1:
        raise ValueError("compatibility admission insertion point is not unique")
    result = source.replace(ANCHOR, GUARD + ANCHOR)
    compile(result, "compatibility-service.py", "exec")
    return result.encode()


if __name__ == "__main__":
    import fs2_serve.scientific_batch.service as service

    target = Path(service.__file__)
    target.write_bytes(guarded_source(target.read_bytes()))
