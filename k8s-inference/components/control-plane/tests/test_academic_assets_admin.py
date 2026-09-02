"""The academic asset operator projection keeps its two axes separate."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fs2_serve.admin import (
    AdminAdapterUnavailableError,
    UnavailableAcademicAssetAdminAdapter,
)
from fs2_serve.admin_models import (
    AcademicAssetReadiness,
    AcademicAssetReadinessList,
    AcademicAssetSnapshot,
    AcademicAssetVolume,
)

CATALOG_PROJECTION = Path(__file__).resolve().parents[3] / "catalog/runtime/contracts/academic-asset-readiness.json"


def volume() -> AcademicAssetVolume:
    return AcademicAssetVolume(
        namespace="fs2-academic-poc",
        claim="academic-assets-runtime-rwx",
        mount_root="/opt/fs2/academic",
    )


def test_null_adapter_fails_closed() -> None:
    adapter = UnavailableAcademicAssetAdminAdapter()
    with pytest.raises(AdminAdapterUnavailableError):
        import asyncio

        asyncio.run(adapter.snapshot())


def test_licensed_bytes_can_never_be_declared_embedded() -> None:
    """The delivery contract is a compile-time invariant, not a convention."""

    with pytest.raises(ValueError):
        AcademicAssetReadiness.model_validate(
            {
                "asset_id": "alphafold3",
                "model_id": "alphafold3",
                "backend_id": "alphafold3-native",
                "display_name": "AlphaFold 3",
                "state": "Ready",
                "use_authorization_status": "Granted",
                "execution_authorization_status": "Authorized",
                "formal_license_status": "FormalAcceptancePending",
                "artifact_status": "ArtifactVerified",
                "tenant_cache_status": "TenantCacheReady",
                "runtime_status": "RuntimeReady",
                "deployment_status": "MissingDeployment",
                "semantic_status": "MissingSemanticReadiness",
                "license_id": "AlphaFold-3-Model-Parameters-Terms-of-Use-2024-11-09",
                "delivery": {
                    "mode": "tenant-private-volume",
                    "mount_path": "/opt/fs2/academic/alphafold3",
                    "embed_in_image": True,
                    "asset_gid": 65532,
                    "consumer_access": "supplemental-group",
                    "install_relative_path": None,
                },
            }
        )


def test_general_shared_cache_and_world_readable_are_refused() -> None:
    for field in ("general_shared_cache", "world_readable"):
        payload = {
            "namespace": "fs2-academic-poc",
            "claim": "academic-assets-runtime-rwx",
            "mount_root": "/opt/fs2/academic",
            field: True,
        }
        with pytest.raises(ValueError):
            AcademicAssetVolume.model_validate(payload)


def test_committed_catalog_projection_parses_into_the_operator_contract() -> None:
    """The BFF payload is the generated catalog projection, not a hand-written claim."""

    document = json.loads(CATALOG_PROJECTION.read_text())
    payload = AcademicAssetReadinessList(
        generation=document["generation"],
        runtime_path_state=document["runtime_path_state"],
        formal_license_state=document["formal_license_state"],
        request_time_license_receipt_required=document["request_time_license_receipt_required"],
        delivery=AcademicAssetVolume(
            namespace=document["delivery"]["namespace"],
            claim=document["delivery"]["claim"],
            mount_root=document["delivery"]["mount_root"],
        ),
        items=[
            AcademicAssetReadiness(
                asset_id=model["asset_id"],
                model_id=model["model_id"],
                backend_id=model["backend_id"],
                display_name=model["display_name"],
                state=model["state"],
                serving_admission=model["serving_admission"],
                use_authorization_status=model["use_authorization_status"],
                execution_authorization_status=model["execution_authorization_status"],
                formal_license_status=model["formal_license_status"],
                artifact_status=model["artifact_status"],
                tenant_cache_status=model["tenant_cache_status"],
                runtime_status=model["runtime_status"],
                deployment_status=model["deployment_status"],
                semantic_status=model["semantic_status"],
                license_id=model["license_id"],
                delivery=model["delivery"],
                artifact_sha256=model["artifact_sha256"],
                runtime_image_digest=model["runtime_image_digest"],
                runtime_environment_digest=model["runtime_environment_digest"],
                alternative=model["alternative"],
            )
            for model in document["models"]
        ],
    )
    assert payload.items, "the projection must carry the gated native models"
    for item in payload.items:
        assert item.delivery.embed_in_image is False
        assert item.delivery.mount_path.startswith("/opt/fs2/academic/")
        # Operational progress must never be reported as licence acceptance...
        assert item.formal_license_status.value == "FormalAcceptancePending"
        # ...and pending paperwork must never block an authorized, ready model.
        assert item.serving_admission.value == "AdmittedNoPerRequestLicenseReceipt"
        assert item.alternative is not None
        assert item.alternative.model_id != item.model_id

    snapshot = AcademicAssetSnapshot(observed_at=datetime.now(UTC), data=payload)
    assert snapshot.data.formal_license_state == "Pending"
    assert snapshot.data.request_time_license_receipt_required is False


def test_a_validation_image_is_never_reported_as_a_published_runtime_image() -> None:
    document = json.loads(CATALOG_PROJECTION.read_text())
    by_id = {model["model_id"]: model for model in document["models"]}
    bindcraft = by_id["bindcraft"]
    assert bindcraft["runtime_image_digest"] is None
    assert bindcraft["runtime_environment_digest"] is not None


def test_licence_terms_never_gate_an_inference_request() -> None:
    document = json.loads(CATALOG_PROJECTION.read_text())
    assert document["request_time_license_receipt_required"] is False
    for model in document["models"]:
        if model["runtime_status"] == "RuntimeReady":
            assert model["serving_admission"] == "AdmittedNoPerRequestLicenseReceipt"


def test_operational_readiness_does_not_imply_formal_acceptance() -> None:
    document = json.loads(CATALOG_PROJECTION.read_text())
    ready = [m for m in document["models"] if m["runtime_status"] == "RuntimeReady"]
    assert ready, "at least one asset should have a proven runtime path"
    for model in ready:
        assert model["formal_license_status"] == "FormalAcceptancePending"
    assert document["formal_license_state"] == "Pending"


def test_catalog_backed_adapter_reads_the_generated_projection(tmp_path: Path) -> None:
    import asyncio
    import shutil

    from fs2_serve.academic_assets import CatalogAcademicAssetAdminAdapter

    contracts = tmp_path / "contracts"
    contracts.mkdir()
    shutil.copy(CATALOG_PROJECTION, contracts / "academic-asset-readiness.json")
    adapter = CatalogAcademicAssetAdminAdapter(tmp_path)
    snapshot = asyncio.run(adapter.snapshot())
    assert snapshot.data.items
    assert snapshot.data.formal_license_state == "Pending"
    assert snapshot.data.request_time_license_receipt_required is False
    assert all(item.delivery.embed_in_image is False for item in snapshot.data.items)


def test_catalog_backed_adapter_fails_closed_when_undelivered(tmp_path: Path) -> None:
    import asyncio

    from fs2_serve.academic_assets import CatalogAcademicAssetAdminAdapter

    adapter = CatalogAcademicAssetAdminAdapter(tmp_path)
    with pytest.raises(AdminAdapterUnavailableError):
        asyncio.run(adapter.snapshot())
