"""One-shot, workload-identity-authenticated artifact authority cutover."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ArtifactAuthorityCutoverSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FS2_ARTIFACT_AUTHORITY_CUTOVER_", extra="forbid")

    issuer_url: str = "https://fs2-artifact-authority.fs2-system.svc:8443/v1"
    audience: str = "fs2-artifact-authority-issuer"
    token_file: Path = Path("/var/run/secrets/fs2-artifact-authority-cutover/token")
    ca_file: Path = Path("/var/run/secrets/fs2-artifact-authority-cutover/ca.crt")
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    retry_deadline_seconds: float = Field(default=240.0, ge=30, le=270)
    retry_interval_seconds: float = Field(default=2.0, ge=0.25, le=10)

    @model_validator(mode="after")
    def validate_issuer(self) -> "ArtifactAuthorityCutoverSettings":
        parsed = urlsplit(self.issuer_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "fs2-artifact-authority.fs2-system.svc"
            or parsed.port != 8443
            or parsed.path.rstrip("/") != "/v1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("artifact authority cutover issuer must be the exact in-cluster service")
        return self


async def close_artifact_authority_cutover(settings: ArtifactAuthorityCutoverSettings) -> dict[str, object]:
    try:
        token = settings.token_file.read_text(encoding="ascii").strip()
    except OSError as error:
        raise RuntimeError("artifact authority cutover identity is unavailable") from error
    if not token or len(token.encode()) > 16 * 1024 or any(character.isspace() for character in token):
        raise RuntimeError("artifact authority cutover identity is invalid")
    try:
        async with httpx.AsyncClient(
            verify=str(settings.ca_file),
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{settings.issuer_url.rstrip('/')}/cutover/close",
                headers={"authorization": f"Bearer {token}"},
                json={"audience": settings.audience},
            )
    except httpx.HTTPError as error:
        raise RuntimeError("artifact authority cutover endpoint is unavailable") from error
    if response.status_code != 200:
        raise RuntimeError("artifact authority cutover was refused")
    try:
        document = response.json()
        closed_at = str(document["closed_at"])
        operation_count = int(document["enrolled_operation_count"])
        input_count = int(document["enrolled_input_count"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("artifact authority cutover receipt is invalid") from None
    if operation_count < 0 or input_count < 0:
        raise RuntimeError("artifact authority cutover receipt is invalid")
    return {
        "closed_at": closed_at,
        "enrolled_operation_count": operation_count,
        "enrolled_input_count": input_count,
    }


def run_artifact_authority_cutover() -> None:
    settings = ArtifactAuthorityCutoverSettings()

    async def close_with_retry() -> dict[str, object]:
        deadline = time.monotonic() + settings.retry_deadline_seconds
        last_error: RuntimeError | None = None
        while time.monotonic() < deadline:
            try:
                return await close_artifact_authority_cutover(settings)
            except RuntimeError as error:
                last_error = error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(settings.retry_interval_seconds, remaining))
        raise RuntimeError("artifact authority cutover retry deadline expired") from last_error

    receipt = asyncio.run(close_with_retry())
    sys.stdout.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")


def run_artifact_authority_cutover_controller() -> None:
    """Retry the idempotent close forever; a Deployment upgrade repairs code."""

    settings = ArtifactAuthorityCutoverSettings()

    async def reconcile() -> None:
        reported = False
        while True:
            try:
                receipt = await close_artifact_authority_cutover(settings)
            except RuntimeError:
                await asyncio.sleep(settings.retry_interval_seconds)
                continue
            if not reported:
                sys.stdout.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
                sys.stdout.flush()
                reported = True
            await asyncio.sleep(60)

    asyncio.run(reconcile())


__all__ = [
    "ArtifactAuthorityCutoverSettings",
    "close_artifact_authority_cutover",
    "run_artifact_authority_cutover",
    "run_artifact_authority_cutover_controller",
]
