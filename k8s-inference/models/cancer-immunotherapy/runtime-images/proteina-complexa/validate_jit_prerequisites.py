#!/usr/bin/env python3
"""Check real C-header compilation, then a tiny CUDA/Triton kernel on GPU."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sysconfig
import tempfile


def validate(cpu_only: bool = False) -> dict:
    include = sysconfig.get_path("include")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "python_header.c"
        source.write_text("#include <Python.h>\nint main(void) { return 0; }\n")
        subprocess.run(["gcc", "-fsyntax-only", "-I" + include, str(source)], check=True)
    result = {"python_header_compile": True, "python_include": include}
    if cpu_only:
        return result

    import torch
    import triton
    import triton.language as tl

    @triton.jit
    def add_one(x, y):
        offsets = tl.arange(0, 64)
        tl.store(y + offsets, tl.load(x + offsets) + 1)

    assert torch.cuda.is_available()
    x = torch.arange(64, device="cuda", dtype=torch.float32)
    y = torch.empty_like(x)
    add_one[(1,)](x, y)
    torch.cuda.synchronize()
    torch.testing.assert_close(y, x + 1, rtol=0, atol=0)
    result.update(triton_jit_kernel=True, device=torch.cuda.get_device_name())
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu-only", action="store_true")
    print(json.dumps(validate(parser.parse_args().cpu_only)))
