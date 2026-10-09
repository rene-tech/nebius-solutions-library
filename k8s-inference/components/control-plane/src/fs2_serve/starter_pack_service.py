"""Independent background seeding using existing, bucket-scoped S3 identities."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from threading import Event
from typing import Any

import boto3
from botocore.config import Config

from .starter_packs import BucketPackInstaller, PackError, StarterPack

LOG = logging.getLogger(__name__)


class StarterPackService:
    def __init__(
        self,
        storage: Any,
        root: Path,
        *,
        poll_seconds: float = 60,
        expected_sha256: str | None = None,
        tenant_ids: tuple[str, ...] = (),
    ) -> None:
        self.storage, self.root, self.poll_seconds = storage, root, poll_seconds
        self.repository = storage.repository
        self.pool = self.repository.pool
        self.pack: StarterPack | None = None
        self.task: asyncio.Task[None] | None = None
        self.load_error: str | None = None
        self.stop = Event()
        self.expected_sha256 = expected_sha256
        self.tenant_ids = frozenset(tenant_ids)

    async def view(self, bucket_id: str) -> dict[str, Any]:
        if self.load_error:
            return {"state": "failed", "error_code": self.load_error}
        if self.pack is None:
            return {"state": "pending"}
        try:
            row = await self.pool.fetchrow(
                """SELECT state,pack_version AS version,manifest_sha256,object_count,total_bytes,
                          attempts,error_code,updated_at,completed_at
                   FROM fs2_customer_starter_packs WHERE bucket_id=$1 AND pack_version=$2""",
                bucket_id,
                self.pack.version,
            )
        except Exception:
            # Seeding/receipt availability must not change bucket readiness.
            return {"state": "failed", "version": self.pack.version, "error_code": "receipt_unavailable"}
        if row and row["manifest_sha256"] != self.pack.digest:
            return {**dict(row), "state": "failed", "error_code": "immutable_version_conflict"}
        return dict(row) if row else {"state": "pending", "version": self.pack.version}

    @staticmethod
    def _install(credentials: Any, bucket: dict[str, Any], pack: StarterPack, stop: Event) -> dict[str, Any]:
        if credentials.bucket_name != bucket["bucket_name"] or credentials.endpoint != bucket["endpoint"]:
            raise PackError("credential_bucket_mismatch")
        # Explicit credentials only: do not consult the SDK/default environment,
        # provisioner identity or shared scientific-result-store credentials.
        client = boto3.client(
            "s3",
            endpoint_url=credentials.endpoint,
            region_name=credentials.region,
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2}),
        )
        try:
            return BucketPackInstaller(
                client,
                bucket["bucket_name"],
                quota_bytes=bucket["quota_bytes"],
                stop=stop,
            ).install(pack)
        finally:
            client.close()

    async def _seed(self, user: Any, bucket: dict[str, Any]) -> None:
        assert self.pack is not None
        pack, bucket_id = self.pack, bucket["bucket_id"]
        # Claim only a new or retryable receipt for the *same immutable pack*.
        # A version collision, completed receipt, or exhausted retry is skipped.
        row = await self.pool.fetchrow(
            """INSERT INTO fs2_customer_starter_packs(bucket_id,pack_version,manifest_sha256,state,attempts)
               VALUES($1,$2,$3,'pending',1)
               ON CONFLICT(bucket_id,pack_version) DO UPDATE
               SET state='pending',attempts=fs2_customer_starter_packs.attempts+1,error_code=NULL,updated_at=now()
               WHERE fs2_customer_starter_packs.state <> 'complete'
                 AND fs2_customer_starter_packs.manifest_sha256=$3
                 AND fs2_customer_starter_packs.attempts < 8
                 AND fs2_customer_starter_packs.updated_at < now()-interval '5 minutes'
               RETURNING bucket_id""",
            bucket_id,
            pack.version,
            pack.digest,
        )
        if row is None:
            return
        try:
            # Refresh enable/policy decisions immediately before disclosing the
            # already-provisioned scoped key; do not create/adopt any identity.
            configured = await self.storage.users.configured(user.tenant_id, user.principal_id)
            policy = await self.storage.policy(user.tenant_id)
            credential = await self.repository.credential(user.tenant_id, user.principal_id)
            if (
                (configured is not None and not configured.enabled)
                or policy.mode == "disabled"
                or not credential
                or not credential["enabled"]
            ):
                raise PackError("storage_disabled")
            credentials = await self.repository.disclose(user.tenant_id, user.principal_id)
            worker = asyncio.create_task(asyncio.to_thread(self._install, credentials, bucket, pack, self.stop))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Do not release the inter-replica lock while the thread still
                # performs S3 I/O. Stop between bounded requests, then join it.
                self.stop.set()
                with suppress(Exception):
                    await worker
                raise
            await self.pool.execute(
                """UPDATE fs2_customer_starter_packs SET state='complete',object_count=$4,total_bytes=$5,
                   error_code=NULL,updated_at=now(),completed_at=now()
                   WHERE bucket_id=$1 AND pack_version=$2 AND manifest_sha256=$3""",
                bucket_id,
                pack.version,
                pack.digest,
                result["object_count"],
                result["total_bytes"],
            )
        except Exception as exc:
            # Provider exceptions can contain signed requests; never persist
            # their text, payloads, keys, tenant names or credential values.
            code = str(exc) if isinstance(exc, PackError) else type(exc).__name__
            await self.pool.execute(
                """UPDATE fs2_customer_starter_packs SET state='partial',error_code=$4,updated_at=now()
                   WHERE bucket_id=$1 AND pack_version=$2 AND manifest_sha256=$3 AND state<>'complete'""",
                bucket_id,
                pack.version,
                pack.digest,
                code,
            )
            LOG.warning("starter pack incomplete bucket_id=%s version=%s code=%s", bucket_id, pack.version, code)

    async def reconcile_once(self) -> None:
        if self.pack is None:
            try:
                candidate = await asyncio.to_thread(StarterPack.load, self.root)
                if self.expected_sha256 is not None and candidate.digest != self.expected_sha256:
                    raise PackError("configured_pack_checksum_mismatch")
                self.pack = candidate
                self.load_error = None
            except Exception as exc:
                self.load_error = str(exc) if isinstance(exc, PackError) else type(exc).__name__
                LOG.warning("starter pack unavailable code=%s", self.load_error)
                return
        # One seeding worker across gateway replicas. It has its own lock and
        # task, independent of provisioning and request/admission processing.
        async with self.pool.acquire() as connection:
            acquired = await connection.fetchval("SELECT pg_try_advisory_lock(24072026, 33)")
            if not acquired:
                return
            try:
                seen: set[str] = set()
                for user in await self.storage.users.list(None):
                    if self.tenant_ids and user.tenant_id not in self.tenant_ids:
                        continue
                    if not user.enabled or (await self.storage.policy(user.tenant_id)).mode == "disabled":
                        continue
                    credential = await self.repository.credential(user.tenant_id, user.principal_id)
                    if not credential or not credential["enabled"]:
                        continue
                    bucket = await self.repository.bucket(user.tenant_id, credential["owner_key"])
                    if not bucket or bucket["bucket_id"] in seen:
                        continue
                    seen.add(bucket["bucket_id"])
                    await self._seed(user, bucket)
            finally:
                await connection.execute("SELECT pg_advisory_unlock(24072026, 33)")

    async def _run(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception as exc:
                LOG.warning("starter pack reconciliation failed type=%s", type(exc).__name__)
            await asyncio.sleep(self.poll_seconds)

    def start(self) -> None:
        self.stop.clear()
        self.task = asyncio.create_task(self._run(), name="customer-starter-packs")

    async def close(self) -> None:
        if self.task:
            self.stop.set()
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
