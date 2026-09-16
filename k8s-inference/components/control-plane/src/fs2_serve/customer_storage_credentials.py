"""Append-only encrypted customer-storage credential repository.

Every encrypt, decrypt, migration, reconciliation, disclosure, and rollback
read passes through :class:`CustomerStorageCrypto`; the only AAD derivation is
``PayloadCipher.customer_storage_aad`` inside that reviewed boundary.  Writes
always append a new generation.  Historical ciphertext and both independent
key IDs remain available until authoritative usage reaches zero.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import asyncpg

from .crypto import Ciphertext, CustomerStorageCrypto


@dataclass(frozen=True)
class CustomerStorageCredentialGeneration:
    tenant_id: str
    principal_id: str
    generation: int
    cipher_key_id: str
    cipher_nonce: bytes
    cipher_value: bytes
    name_key_id: str
    name_digest: str
    migration_from_generation: int | None

    def envelope(self) -> Ciphertext:
        return Ciphertext(
            key_id=self.cipher_key_id,
            nonce=self.cipher_nonce,
            value=self.cipher_value,
        )


class PostgresCustomerStorageCredentialRepository:
    """Append-only persistence with transaction-serialized generations."""

    def __init__(self, pool: asyncpg.Pool[Any], crypto: CustomerStorageCrypto) -> None:
        self.pool = pool
        self.crypto = crypto

    @staticmethod
    def _validate_identity(tenant_id: str, principal_id: str) -> None:
        # This invokes the single canonical identity/AAD validator without
        # exposing or duplicating the AAD literal.
        from .crypto import PayloadCipher

        PayloadCipher.customer_storage_aad(tenant_id, principal_id)

    @staticmethod
    def _advisory_key(tenant_id: str, principal_id: str) -> int:
        raw = hashlib.sha256(f"{tenant_id}\0{principal_id}".encode()).digest()[:8]
        return int.from_bytes(raw, "big", signed=True)

    @staticmethod
    def _record(row: asyncpg.Record) -> CustomerStorageCredentialGeneration:
        return CustomerStorageCredentialGeneration(
            tenant_id=row["tenant_id"],
            principal_id=row["principal_id"],
            generation=row["generation"],
            cipher_key_id=row["cipher_key_id"],
            cipher_nonce=bytes(row["cipher_nonce"]),
            cipher_value=bytes(row["cipher_value"]),
            name_key_id=row["name_key_id"],
            name_digest=row["name_digest"],
            migration_from_generation=row["migration_from_generation"],
        )

    async def append_current_write(
        self,
        *,
        project_id: str,
        tenant_id: str,
        principal_id: str,
        plaintext: bytes,
        migration_from_generation: int | None = None,
    ) -> CustomerStorageCredentialGeneration:
        self._validate_identity(tenant_id, principal_id)
        if not isinstance(plaintext, bytes) or not plaintext:
            raise ValueError("customer storage credential must be non-empty bytes")
        envelope = self.crypto.encrypt_secret(
            plaintext, tenant_id=tenant_id, principal_id=principal_id
        )
        name_key_id, name_digest = self.crypto.bucket_name_digest(
            project_id=project_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1)",
                self._advisory_key(tenant_id, principal_id),
            )
            generation = await connection.fetchval(
                """
                SELECT COALESCE(MAX(generation), 0) + 1
                FROM fs2_customer_storage_credential_generations
                WHERE tenant_id = $1 AND principal_id = $2
                """,
                tenant_id,
                principal_id,
            )
            if migration_from_generation is not None and migration_from_generation >= generation:
                raise ValueError("migration predecessor must be an older generation")
            row = await connection.fetchrow(
                """
                INSERT INTO fs2_customer_storage_credential_generations (
                    tenant_id, principal_id, generation,
                    cipher_key_id, cipher_nonce, cipher_value,
                    name_key_id, name_digest, migration_from_generation
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                RETURNING tenant_id, principal_id, generation,
                          cipher_key_id, cipher_nonce, cipher_value,
                          name_key_id, name_digest, migration_from_generation
                """,
                tenant_id,
                principal_id,
                generation,
                envelope.key_id,
                envelope.nonce,
                envelope.value,
                name_key_id,
                name_digest,
                migration_from_generation,
            )
        if row is None:
            raise RuntimeError("customer storage credential append returned no row")
        return self._record(row)

    async def generation(
        self, *, tenant_id: str, principal_id: str, generation: int | None = None
    ) -> CustomerStorageCredentialGeneration | None:
        self._validate_identity(tenant_id, principal_id)
        if generation is not None and generation < 1:
            raise ValueError("customer storage generation must be positive")
        query = """
            SELECT tenant_id, principal_id, generation,
                   cipher_key_id, cipher_nonce, cipher_value,
                   name_key_id, name_digest, migration_from_generation
            FROM fs2_customer_storage_credential_generations
            WHERE tenant_id = $1 AND principal_id = $2
        """
        arguments: tuple[Any, ...] = (tenant_id, principal_id)
        if generation is None:
            query += " ORDER BY generation DESC LIMIT 1"
        else:
            query += " AND generation = $3"
            arguments = (*arguments, generation)
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(query, *arguments)
        return None if row is None else self._record(row)

    async def decrypt(
        self, *, tenant_id: str, principal_id: str, generation: int | None = None
    ) -> bytes:
        record = await self.generation(
            tenant_id=tenant_id,
            principal_id=principal_id,
            generation=generation,
        )
        if record is None:
            raise KeyError("customer storage credential is absent")
        return self.crypto.decrypt_secret(
            record.envelope(), tenant_id=tenant_id, principal_id=principal_id
        )

    async def migrate_current(
        self, *, project_id: str, tenant_id: str, principal_id: str
    ) -> CustomerStorageCredentialGeneration:
        current = await self.generation(
            tenant_id=tenant_id, principal_id=principal_id
        )
        if current is None:
            raise KeyError("customer storage credential is absent")
        plaintext = self.crypto.decrypt_secret(
            current.envelope(), tenant_id=tenant_id, principal_id=principal_id
        )
        return await self.append_current_write(
            project_id=project_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            plaintext=plaintext,
            migration_from_generation=current.generation,
        )

    async def key_usage(self) -> dict[str, dict[str, int]]:
        """Authoritative zero-usage input; this method never retires a key."""

        async with self.pool.acquire() as connection:
            cipher_rows = await connection.fetch(
                """
                SELECT cipher_key_id AS key_id, COUNT(*)::bigint AS uses
                FROM fs2_customer_storage_credential_generations
                GROUP BY cipher_key_id ORDER BY cipher_key_id
                """
            )
            name_rows = await connection.fetch(
                """
                SELECT name_key_id AS key_id, COUNT(*)::bigint AS uses
                FROM fs2_customer_storage_credential_generations
                GROUP BY name_key_id ORDER BY name_key_id
                """
            )
        return {
            "cipher": {row["key_id"]: row["uses"] for row in cipher_rows},
            "name": {row["key_id"]: row["uses"] for row in name_rows},
        }


class CustomerStorageCredentialReconciler:
    def __init__(self, repository: PostgresCustomerStorageCredentialRepository) -> None:
        self.repository = repository

    async def write(
        self,
        *,
        project_id: str,
        tenant_id: str,
        principal_id: str,
        credential: bytes,
    ) -> CustomerStorageCredentialGeneration:
        return await self.repository.append_current_write(
            project_id=project_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            plaintext=credential,
        )

    async def migrate(
        self, *, project_id: str, tenant_id: str, principal_id: str
    ) -> CustomerStorageCredentialGeneration:
        return await self.repository.migrate_current(
            project_id=project_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )


class CustomerStorageCredentialDisclosure:
    def __init__(self, repository: PostgresCustomerStorageCredentialRepository) -> None:
        self.repository = repository

    async def disclose_to_same_principal(
        self,
        *,
        authenticated_tenant_id: str,
        authenticated_principal_id: str,
        tenant_id: str,
        principal_id: str,
        generation: int | None = None,
    ) -> bytes:
        if (
            authenticated_tenant_id != tenant_id
            or authenticated_principal_id != principal_id
        ):
            raise PermissionError("customer storage credential scope mismatch")
        return await self.repository.decrypt(
            tenant_id=tenant_id,
            principal_id=principal_id,
            generation=generation,
        )
