"""Locate the immutable catalog data installed beside the Python distribution."""

from __future__ import annotations

import sysconfig
from pathlib import Path

from .loader import CatalogError


def installed_catalog_root() -> Path:
    """Return the wheel-installed static catalog root, failing closed if incomplete."""

    root = Path(sysconfig.get_path("data")) / "share" / "fs2-serve" / "catalog"
    required = (
        root / "catalog.json",
        root / "pyproject.toml",
        root / "uv.lock",
        root / "contracts" / "gateway-consumer.fixture.json",
        root / "contracts" / "model-variants.json",
        root / "contracts" / "model-variant-consumer.fixture.json",
        root / "contracts" / "scale-contracts.json",
        root / "schema" / "scale-contracts.schema.json",
        root / "schema" / "postgres-activation-intent.schema.json",
        root / "schema" / "model-variants.schema.json",
        root / "schema" / "model-variant-supply-receipt.schema.json",
        root / "schema" / "model-variant-supply-object.schema.json",
        root / "schema" / "model-variant-attestor-policy.schema.json",
        root / "schema" / "model-variant-qualification-receipt.schema.json",
        root / "schema" / "model-variant-promotions.schema.json",
        root / "schema" / "model-variant-runtime-tuple.schema.json",
        root / "schema" / "model-variant-semantic-receipt.schema.json",
        root / "schema" / "model-variant-cohort.schema.json",
        root / "schema" / "model-variant-backend-readiness-receipt.schema.json",
        root / "schema" / "model-variant-kubernetes-observation.schema.json",
        root / "schema" / "model-variant-cold-boundary-receipt.schema.json",
        root / "schema" / "model-variant-preemption-receipt.schema.json",
        root / "schema" / "model-variant-lifecycle-receipt.schema.json",
        root / "schema" / "model-variant-review-receipt.schema.json",
        root / "schema" / "protected-storage-class-receipt.schema.json",
        root / "schema" / "provider-block-writer-admission.schema.json",
        root / "schema" / "provider-block-pvc-lifecycle-receipt.schema.json",
        root / "schema" / "serving-bindings.schema.json",
        root / "schema" / "zero-to-ready-receipt.schema.json",
        root / "schema" / "return-to-zero-receipt.schema.json",
        root / "schema" / "runtime-startup-receipt.schema.json",
        root / "schema" / "replica-field-ownership-receipt.schema.json",
        root / "sql" / "0001_activation_store.sql",
        root / "models" / "qwen3-8b.json",
        root / "validators" / "validate_response.py",
        root / "validators" / "assets" / "qwen3-8b.json",
        root
        / "repository"
        / "nim-fast-start"
        / "faststart-v2"
        / "validate_openfold2.py",
    )
    if not root.is_dir() or root.is_symlink() or any(
        not path.is_file() or path.is_symlink() for path in required
    ):
        raise CatalogError("installed fs2-serve catalog data is absent or incomplete")
    return root
