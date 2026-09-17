from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_apply_gate_binds_plan_server_time_cas_and_metadata_only_secrets() -> None:
    source = (ROOT / "tools/apply_nim_security_plan.py").read_text()

    assert 'hashlib.sha256(plan.read_bytes()).hexdigest()' in source
    assert 'server_date = response.headers.get("Date")' in source
    assert 'application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1' in source
    assert 'metadata.get("resourceVersion") != lease_resource_version' in source
    assert '[args.terraform, "apply", str(plan)]' in source
    assert 'kubernetes.request("PUT", path, body=lease)' in source
    assert 'request("DELETE"' not in source
