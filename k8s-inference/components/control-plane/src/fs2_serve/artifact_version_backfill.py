"""Evidence-backed transition for pre-versioned scientific artifacts.

The operator supplies an exact artifact/version pair through a root-owned
manifest.  This process cannot enumerate a bucket or select a latest object.
For each row it obtains a maintenance-only, exact-version read session from the
independent broker, hashes the complete immutable provider version, verifies
all finalized metadata, and invokes the one-way database function.  Operations
remain purge-fenced while any artifact version is unresolved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .artifact_credential_broker import ArtifactCredentialBroker, ArtifactCredentialBrokerConfig
from .scientific_artifacts import ArtifactCompression, VerifiedStoredObject


class ArtifactVersionBackfillSettings(BaseSettings):
    """Non-provider-secret inputs for the isolated maintenance process."""

    model_config = SettingsConfigDict(env_prefix="FS2_ARTIFACT_BACKFILL_", extra="forbid")

    database_url: str
    manifest_file: Path
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    broker_url_template: str
    broker_audience: str = "fs2-artifact-credential-broker"
    broker_token_file: Path
    broker_ca_file: Path
    broker_readiness_bindings_file: Path
    broker_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    broker_operation_timeout_seconds: int = Field(default=120, ge=30, le=900)


@dataclass(frozen=True, slots=True)
class BackfillCandidate:
    artifact_id: UUID
    object_version_id: str


def load_backfill_manifest(path: Path, *, expected_sha256: str) -> tuple[BackfillCandidate, ...]:
    """Load only explicit artifact/version pairs; never accept key-derived authority."""

    try:
        payload = path.read_bytes()
        if not hashlib.sha256(payload).hexdigest() == expected_sha256:
            raise ValueError("artifact version backfill manifest digest differs")
        document = json.loads(payload)
    except (OSError, ValueError) as error:
        raise ValueError("artifact version backfill manifest is unavailable") from error
    if not isinstance(document, dict) or set(document) != {"schema", "candidates"}:
        raise ValueError("artifact version backfill manifest is invalid")
    if document["schema"] != "fs2-serve.nebius.ai/artifact-version-backfill/v1":
        raise ValueError("artifact version backfill manifest schema is invalid")
    if not isinstance(document["candidates"], list) or not document["candidates"]:
        raise ValueError("artifact version backfill manifest has no candidates")
    candidates: list[BackfillCandidate] = []
    seen: set[UUID] = set()
    for value in document["candidates"]:
        if not isinstance(value, dict) or set(value) != {"artifact_id", "object_version_id"}:
            raise ValueError("artifact version backfill candidate is invalid")
        artifact_id = UUID(str(value["artifact_id"]))
        version = value["object_version_id"]
        if (
            not isinstance(version, str)
            or not 1 <= len(version) <= 1024
            or version == "null"
            or any(character.isspace() for character in version)
        ):
            raise ValueError("artifact version backfill candidate version is invalid")
        if artifact_id in seen:
            raise ValueError("artifact version backfill manifest repeats an artifact")
        seen.add(artifact_id)
        candidates.append(BackfillCandidate(artifact_id, version))
    return tuple(candidates)


async def backfill_artifact_versions(settings: ArtifactVersionBackfillSettings) -> None:
    """Measure and record every explicit version in one serialized transaction each."""

    broker = ArtifactCredentialBroker(
        ArtifactCredentialBrokerConfig(
            url_template=settings.broker_url_template,
            audience=settings.broker_audience,
            token_file=settings.broker_token_file,
            ca_file=settings.broker_ca_file,
            timeout_seconds=settings.broker_timeout_seconds,
            operation_timeout_seconds=settings.broker_operation_timeout_seconds,
            readiness_bindings_file=settings.broker_readiness_bindings_file,
        )
    )
    pool = await asyncpg.create_pool(
        dsn=settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
        min_size=1,
        max_size=1,
        command_timeout=30,
        server_settings={"application_name": "fs2-artifact-version-backfill"},
    )
    assert pool is not None
    try:
        for candidate in load_backfill_manifest(
            settings.manifest_file,
            expected_sha256=settings.manifest_sha256,
        ):
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT artifact.id,artifact.operation_id,artifact.tenant_id,artifact.storage_key,"
                    "artifact.object_version_id,artifact.digest,artifact.size_bytes,"
                    "artifact.media_type,artifact.compression,"
                    "backfill.object_version_id AS backfill_object_version_id,"
                    "backfill.verified_digest AS backfill_digest,"
                    "backfill.verified_size_bytes AS backfill_size_bytes,"
                    "backfill.verified_media_type AS backfill_media_type,"
                    "backfill.verified_compression AS backfill_compression,"
                    "backfill.provider_receipt AS backfill_provider_receipt,"
                    "backfill.provider_receipt_digest AS backfill_provider_receipt_digest "
                    "FROM fs2_scientific_artifacts artifact "
                    "LEFT JOIN fs2_scientific_artifact_version_backfills backfill "
                    "ON backfill.artifact_id=artifact.id WHERE artifact.id=$1",
                    candidate.artifact_id,
                )
                if row is None:
                    raise RuntimeError("artifact version backfill candidate is unknown")
                if row["object_version_id"] is not None:
                    exact_replay = (
                        str(row["object_version_id"]) == candidate.object_version_id
                        and str(row["backfill_object_version_id"]) == candidate.object_version_id
                        and row["backfill_digest"] == row["digest"]
                        and row["backfill_size_bytes"] == row["size_bytes"]
                        and row["backfill_media_type"] == row["media_type"]
                        and row["backfill_compression"] == row["compression"]
                        and str(row["backfill_provider_receipt"]).startswith(
                            "fs2_provider_observation."
                        )
                        and row["backfill_provider_receipt_digest"]
                        == "sha256:"
                        + hashlib.sha256(str(row["backfill_provider_receipt"]).encode()).hexdigest()
                    )
                    if exact_replay:
                        continue
                    raise RuntimeError("artifact version backfill candidate has no exact immutable proof")
                try:
                    result = await broker.operation(
                        broker._request(
                            tenant_id=str(row["tenant_id"]),
                            storage_key=str(row["storage_key"]),
                            action="backfill-inspect",
                            object_version_id=candidate.object_version_id,
                            minimum_ttl_seconds=120,
                        )
                    )
                    measured = VerifiedStoredObject.model_validate(result["result"])
                except (KeyError, ValueError) as error:
                    raise RuntimeError("artifact version backfill broker result is invalid") from error
                expected_compression = (
                    None if row["compression"] is None else ArtifactCompression(str(row["compression"]))
                )
                if (
                    measured.object_version_id != candidate.object_version_id
                    or measured.digest != str(row["digest"])
                    or measured.size_bytes != int(row["size_bytes"])
                    or measured.media_type != str(row["media_type"])
                    or measured.compression != expected_compression
                ):
                    raise RuntimeError("artifact version backfill provider proof differs from finalized metadata")
                provider_receipt = result.get("provider_observation")
                if (
                    not isinstance(provider_receipt, str)
                    or not provider_receipt.startswith("fs2_provider_observation.")
                    or len(provider_receipt.encode()) > 16 * 1024
                ):
                    raise RuntimeError("artifact version backfill broker omitted its signed receipt")
                persisted = await connection.fetchrow(
                    "SELECT artifact.object_version_id,backfill.provider_receipt,"
                    "backfill.provider_receipt_digest FROM fs2_scientific_artifacts artifact "
                    "JOIN fs2_scientific_artifact_version_backfills backfill "
                    "ON backfill.artifact_id=artifact.id WHERE artifact.id=$1",
                    row["id"],
                )
                if (
                    persisted is None
                    or str(persisted["object_version_id"]) != candidate.object_version_id
                    or persisted["provider_receipt"] != provider_receipt
                    or persisted["provider_receipt_digest"]
                    != "sha256:" + hashlib.sha256(provider_receipt.encode()).hexdigest()
                ):
                    raise RuntimeError("artifact version backfill issuer receipt was not retained")
    finally:
        await broker.close()
        await pool.close()


def run_artifact_version_backfill() -> None:
    import asyncio

    asyncio.run(backfill_artifact_versions(ArtifactVersionBackfillSettings()))  # type: ignore[call-arg]


__all__ = [
    "ArtifactVersionBackfillSettings",
    "BackfillCandidate",
    "backfill_artifact_versions",
    "load_backfill_manifest",
    "run_artifact_version_backfill",
]
