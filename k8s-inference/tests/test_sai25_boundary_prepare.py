from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "tools/prepare_nim_security_boundary.py"
    spec = importlib.util.spec_from_file_location("prepare_nim_security_boundary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase_zero_proposal_contains_source_bytes_only() -> None:
    value = _module().proposal()

    assert value["source_only"] is True
    assert set(value) == {
        "schema",
        "source_only",
        "chart",
        "chart_tree_sha256",
        "files",
        "fixed_release",
        "external_inputs_required_after_phase_zero",
        "forbidden_phase_zero_inputs",
    }
    encoded = repr(value)
    for forbidden in (
        "authorization_id",
        "attestation",
        "policy_uid",
        "resource_version",
        "secret_uid",
        "installation_receipt",
    ):
        assert forbidden not in encoded
