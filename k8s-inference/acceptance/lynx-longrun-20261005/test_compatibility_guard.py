import textwrap
from pathlib import Path

import pytest

from install_compatibility_guard import GUARD, guarded_source


def test_patch_only_changes_frozen_admission_and_requires_exact_legacy_code():
    root = Path(__file__).resolve().parents[2]
    source = (root / "components/control-plane/src/fs2_serve/scientific_batch/service.py").read_bytes()
    result = guarded_source(source)
    assert result != source
    assert result.replace(GUARD.encode(), b"") == source
    with pytest.raises(ValueError, match="expected legacy"):
        guarded_source(source + b"\n")


@pytest.mark.parametrize("field", ["input_manifest", "adapter_execution"])
def test_compatibility_admission_rejects_new_encoding_before_commit(field):
    scope = {"ScientificProfileError": ValueError, "payload": {
        "input_manifest": {"entries": []}, "adapter_execution": {"invocations": []},
    }}
    code = compile(textwrap.dedent(GUARD), "compatibility-guard", "exec")
    exec(code, scope)  # noqa: S102 - exact local literal under test
    scope["payload"][field] = {"encoding": "fs2-serve.nebius.ai/scientific-immutable-metadata/zstd-v1"}
    with pytest.raises(ValueError, match="compatibility rollout"):
        exec(code, scope)  # noqa: S102 - exact local literal under test
