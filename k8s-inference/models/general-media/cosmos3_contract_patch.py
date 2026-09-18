"""Narrow, source-pinned Cosmos3 transfer sizing correction.

The hosted API already sets use_resolution_template=false and supplies explicit
dimensions. Upstream 9c1b7504b ignores those for transfer only. Preserve its
default bucket policy; honor the opt-out without resizing a generated camera
image after inference. This patch changes no weights, precision or kernels.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

UPSTREAM_SHA256 = "8a70b5d446315d2f6281bfacb4c332dac17cf256f813cbd26c4e01a105d41f49"
TARGET = Path("/usr/local/lib/python3.12/dist-packages/vllm_omni/diffusion/models/cosmos3/pipeline_cosmos3.py")
ANCHOR = """        resolution = self._get_sp_param(sp, "resolution", self._get_sp_param(sp, "image_size", 720))
        source_h, source_w = source_hw or (COSMOS3_T2V_DEFAULT_HEIGHT, COSMOS3_T2V_DEFAULT_WIDTH)
        target_w, target_h = find_closest_target_size(int(source_h), int(source_w), resolution)
        return int(target_h), int(target_w)
"""
REPLACEMENT = (
    """        if not self._get_sp_param(sp, "use_resolution_template", True):
            height, width = int(sp.height or 0), int(sp.width or 0)
            if height <= 0 or width <= 0 or height % 16 or width % 16:
                raise ValueError("Explicit Cosmos3 transfer dimensions must be positive multiples of 16.")
            return height, width
"""
    + ANCHOR
)


def patch_source(raw: bytes) -> bytes:
    if hashlib.sha256(raw).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Cosmos3 upstream source differs from the reviewed pinned image")
    text = raw.decode()
    if text.count(ANCHOR) != 1:
        raise ValueError("Cosmos3 transfer sizing patch anchor is not unique")
    result = text.replace(ANCHOR, REPLACEMENT, 1)
    compile(result, str(TARGET), "exec")
    return result.encode()


if __name__ == "__main__":
    TARGET.write_bytes(patch_source(TARGET.read_bytes()))
