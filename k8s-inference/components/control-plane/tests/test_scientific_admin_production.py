"""Production feature gating for the reconciled scientific admin surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fs2_serve.scientific_admin_catalog import ScientificProfileDiscoveryAdapter
from fs2_serve.scientific_admin_postgres import (
    PostgresScientificArtifactAdminAdapter,
    PostgresScientificRunAdminAdapter,
    postgres_scientific_admin_read_service,
)

DELIVERED_CATALOG = Path(__file__).resolve().parents[3] / "catalog/runtime"


def _service(*, artifact_service: object | None):
    return postgres_scientific_admin_read_service(
        pool=cast(Any, object()),
        registry=cast(Any, object()),
        catalog_dir=DELIVERED_CATALOG,
        artifact_service=cast(Any, artifact_service),
        scientific_batches=None,
        source_max_age_seconds=90,
        adapter_timeout_seconds=2,
    )


def test_production_startup_binds_the_durable_controller_and_tenant_discovery() -> None:
    service = _service(artifact_service=None)

    assert isinstance(service.models, ScientificProfileDiscoveryAdapter)
    assert isinstance(service.runs, PostgresScientificRunAdminAdapter)
    assert service.artifacts is None
    capabilities = service.capabilities()
    assert capabilities.model_readiness.available is True
    assert capabilities.run_history.available is True
    assert capabilities.artifacts.available is False


def test_artifact_capability_requires_the_real_result_service() -> None:
    service = _service(artifact_service=object())

    assert isinstance(service.artifacts, PostgresScientificArtifactAdminAdapter)
    capabilities = service.capabilities()
    assert capabilities.run_history.available is True
    assert capabilities.artifacts.available is True
