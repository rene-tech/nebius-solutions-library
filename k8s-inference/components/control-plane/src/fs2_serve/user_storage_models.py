"""Customer bucket policy and deliberately separate credential disclosure."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from .models import StrictModel

StorageMode = Literal["tenant", "user", "disabled"]


class StoragePolicy(StrictModel):
    mode: StorageMode = "user"
    quota_bytes: int = Field(default=5_000_000_000, gt=0)


class UserStorage(StrictModel):
    state: Literal["pending", "ready", "disabled", "revoked", "expired", "not_configured"]
    mode: StorageMode = "user"
    quota_bytes: int = 5_000_000_000
    bucket_name: str | None = None
    endpoint: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    expires_at: AwareDatetime | None = None
    # No secret on list/detail responses. It is available on an explicit,
    # authenticated no-store disclosure endpoint, backed by encrypted storage.


class StorageCredentials(StrictModel):
    bucket_name: str
    endpoint: str
    region: str
    access_key_id: str
    secret_access_key: str = Field(repr=False)
    expires_at: AwareDatetime
