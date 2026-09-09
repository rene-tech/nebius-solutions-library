"""The wheel must retain the exact JSON resources used by typed MCP tools."""

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile

from conftest import CONTROL_ROOT


def test_built_wheel_keeps_input_contract_resources_importable_without_source_tree(tmp_path):
    uv = shutil.which("uv")
    assert uv is not None, "the repository build tool uv is required"
    subprocess.run(  # noqa: S603 - fixed build tool and test-owned output directory.
        [uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=CONTROL_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    (wheel,) = (tmp_path / "dist").glob("fs2_serve_control_plane-*.whl")
    source = CONTROL_ROOT / "src/fs2_serve/model_input_schemas"
    expected = {path.name: path.read_bytes() for path in source.glob("*.json")}
    assert {
        "runtime-pydantic.json",
        "runtime-adapters.json",
        "cosmos.json",
        "evo2-source.json",
        "vllm-chat.json",
        "scientific-examples.json",
    } <= expected.keys()
    with zipfile.ZipFile(wheel) as archive:
        for name, raw in expected.items():
            assert archive.read(f"fs2_serve/model_input_schemas/{name}") == raw
            assert isinstance(json.loads(raw), dict)
        for module in ("model_input_contracts.py", "mcp_input_contracts.py", "mcp_server.py"):
            assert archive.getinfo(f"fs2_serve/{module}").file_size > 0
    # A fresh isolated Python imports the package from the wheel itself. Neither
    # repository PYTHONPATH nor an editable package can satisfy these reads.
    script = (
        "import hashlib,importlib.resources,json,sys; "
        "sys.path.insert(0,sys.argv[1]); import fs2_serve; "
        "assert sys.argv[1] in fs2_serve.__file__; "
        "root=importlib.resources.files('fs2_serve').joinpath('model_input_schemas'); "
        "print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() "
        "for p in root.iterdir() if p.name.endswith('.json')},sort_keys=True))"
    )
    loaded = subprocess.run(  # noqa: S603 - fixed interpreter/script and locally built wheel.
        [sys.executable, "-I", "-S", "-c", script, str(wheel)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(loaded.stdout) == {name: hashlib.sha256(raw).hexdigest() for name, raw in expected.items()}
