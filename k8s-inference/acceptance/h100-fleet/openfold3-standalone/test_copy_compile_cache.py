import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def test_cache_copy_preserves_ninja_timestamps_and_never_replaces_different_bytes(tmp_path):
    spec = importlib.util.spec_from_file_location("of3_compile_copy", Path(__file__).with_name("copy_compile_cache.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source, destination = tmp_path / "source", tmp_path / "destination"
    relative = "image/driver-sm"
    (source / relative).mkdir(parents=True)
    binary = source / relative / "native.so"
    binary.write_bytes(b"exact compiled kernel")
    os.utime(binary, ns=(1234567890000000000, 1234567890000000000))
    code = module.COPY.replace("'/source'", repr(str(source))).replace("'/destination'", repr(str(destination)))
    result = subprocess.run([sys.executable, "-c", code, relative], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    copied = destination / relative / "native.so"
    assert copied.read_bytes() == binary.read_bytes()
    assert copied.stat().st_mtime_ns == binary.stat().st_mtime_ns
    copied.write_bytes(b"different active cache")
    result = subprocess.run([sys.executable, "-c", code, relative], capture_output=True, text=True)
    assert result.returncode != 0
    assert copied.read_bytes() == b"different active cache"
