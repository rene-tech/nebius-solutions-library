"""Catalog-backed reader for licensed academic asset readiness.

The readiness projection is generated from the private academic asset state
machine and delivered with the catalog, so the operator API reports observed
state rather than a hand-written claim.  Only identities and readiness state
cross this boundary: never licensed bytes, credentials, owner-only paths or
acceptance receipt bodies.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .admin import AdminAdapterUnavailableError
from .admin_models import (
    AcademicAssetReadiness,
    AcademicAssetReadinessList,
    AcademicAssetSnapshot,
    AcademicAssetVolume,
)

PROJECTION_SCHEMA = "fs2-serve.nebius.ai/academic-asset-readiness/v1"
PROJECTION_FILENAME = "academic-asset-readiness.json"
MAX_PROJECTION_BYTES = 1024 * 1024


class CatalogAcademicAssetAdminAdapter:
    """Reads the generated projection from the delivered catalog directory."""

    def __init__(self, catalog_root: Path) -> None:
        self._path = Path(catalog_root) / "contracts" / PROJECTION_FILENAME

    async def snapshot(self) -> AcademicAssetSnapshot:
        try:
            info = self._path.stat()
        except OSError as error:
            raise AdminAdapterUnavailableError("academic asset projection is not delivered") from error
        if info.st_size > MAX_PROJECTION_BYTES:
            raise AdminAdapterUnavailableError("academic asset projection exceeds its size bound")
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise AdminAdapterUnavailableError("academic asset projection is unreadable") from error
        if not isinstance(document, dict) or document.get("schema") != PROJECTION_SCHEMA:
            raise AdminAdapterUnavailableError("academic asset projection schema is unsupported")

        delivery = document["delivery"]
        payload = AcademicAssetReadinessList(
            generation=document.get("generation"),
            runtime_path_state=document["runtime_path_state"],
            formal_license_state=document["formal_license_state"],
            request_time_license_receipt_required=document["request_time_license_receipt_required"],
            delivery=AcademicAssetVolume(
                namespace=delivery["namespace"],
                claim=delivery["claim"],
                mount_root=delivery["mount_root"],
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
        observed_at = datetime.fromtimestamp(info.st_mtime, tz=UTC)
        return AcademicAssetSnapshot(observed_at=observed_at, data=payload)
