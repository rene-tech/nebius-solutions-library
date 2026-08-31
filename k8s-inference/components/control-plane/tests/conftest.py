from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

CONTROL_ROOT = Path(__file__).resolve().parents[1]
SOLUTION_ROOT = CONTROL_ROOT.parents[1]
CATALOG_ROOT = SOLUTION_ROOT / "catalog/runtime"
REPO_ROOT = CATALOG_ROOT / "packaged-repository"
sys.path.insert(0, str(CONTROL_ROOT / "src"))
sys.path.insert(0, str(CATALOG_ROOT))

from fs2_serve_catalog.loader import load_catalog  # noqa: E402

from fs2_serve.crypto import KeyedHasher, PayloadCipher  # noqa: E402
from fs2_serve.registry import Registry  # noqa: E402


def _canonical_fixture_builder() -> Any:
    """Use the models lane's executable consumer fixture, not a gateway-owned clone."""

    fixture_path = CATALOG_ROOT / "tests" / "test_consumer.py"
    spec = importlib.util.spec_from_file_location("_fs2_catalog_consumer_contract", fixture_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical consumer contract fixture is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    builder = module.GatewayConsumerTests()
    builder.setUp()
    return builder


def promote_qwen(catalog_root: Path) -> None:
    builder = _canonical_fixture_builder()
    builder.promote_qwen(catalog_root, hashlib.sha256(b"control-plane-disabled-binding").hexdigest())
    builder.refresh_scale_contract(catalog_root, "qwen3-8b")


def qwen_binding(catalog_root: Path, *, enabled: bool = True) -> dict[str, Any]:
    catalog = load_catalog(catalog_root, repo_root=REPO_ROOT)
    if enabled:
        raise ValueError("enabled fixture requires canonical immutable evidence")
    return _canonical_fixture_builder().binding_value(catalog, enabled=False)


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    catalog_root = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, catalog_root)
    builder = _canonical_fixture_builder()
    catalog, evidence_root, qualification, _ = builder.live_fixture(catalog_root)
    builder.secure_evidence_tree(evidence_root)
    binding = builder.binding_value(catalog, qualification)
    binding_path = tmp_path / "serving-bindings.json"
    binding_path.write_text(json.dumps(binding) + "\n", encoding="utf-8")
    return Registry.load(
        catalog_root,
        binding_path,
        repo_root=REPO_ROOT,
        evidence_root=evidence_root,
        trusted_attestors=builder.trusted_attestors,
        validation_time=builder.validation_time,
        max_attempts=2,
        max_gpu_seconds_per_attempt=5,
        retry_base_seconds=0.01,
    )


@pytest.fixture
def cipher() -> PayloadCipher:
    return PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": b"p" * 32})


@pytest.fixture
def hasher() -> KeyedHasher:
    return KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"h" * 32})
