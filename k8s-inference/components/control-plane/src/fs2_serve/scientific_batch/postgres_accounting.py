"""Single PostgreSQL paths for scientific interruption and terminal accounting."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import asyncpg

from .codec import state_from_value, state_to_json
from .models import ScientificBatchState


async def lock_scientific_terminal_token(
    connection: asyncpg.Connection[Any], operation_id: UUID
) -> UUID | None:
    """Acquire the global token-before-batch lock order for a terminal write."""

    token_id = await connection.fetchval(
        "SELECT token_id FROM fs2_operations WHERE id=$1 AND protocol='scientific-batch-v1'",
        operation_id,
    )
    if token_id is None:
        return None
    await connection.execute(
        "SELECT pg_advisory_xact_lock("
        "hashtextextended('fs2-scientific-token' || chr(31) || $1::text,0))",
        token_id,
    )
    token = await connection.fetchval("SELECT id FROM fs2_tokens WHERE id=$1 FOR UPDATE", token_id)
    if token is None:
        raise RuntimeError("scientific-batch token disappeared before GPU settlement")
    return token_id


async def project_terminal_scientific_operation(
    connection: asyncpg.Connection[Any], state: ScientificBatchState
) -> None:
    """Invoke the sole database-owned terminal settlement projection."""

    if not state.status.terminal:
        raise ValueError("scientific terminal projection requires terminal batch state")
    await connection.execute(
        "SELECT fs2_scientific_settle_terminal_operation($1)",
        state.operation_id,
    )


async def account_interruption_and_request_cancel(
    connection: asyncpg.Connection[Any], operation_id: UUID, *, cause: str
) -> bool:
    """Persist one generic interruption and leave accounting to the controller.

    Neither the public operation nor its reservation is changed here. Absence
    of a persisted scheduling observation cannot prove absence of external
    execution, so the batch controller must resolve deterministic workload
    ownership, release resources, and invoke terminal settlement later.
    """

    token_id = await lock_scientific_terminal_token(connection, operation_id)
    if token_id is None:
        return False
    batch = await connection.fetchrow(
        "SELECT * FROM fs2_scientific_batches WHERE operation_id=$1 FOR UPDATE",
        operation_id,
    )
    if batch is None:
        raise RuntimeError("scientific operation has no batch state")
    state = state_from_value(batch["state"])
    if state.status.terminal:
        await project_terminal_scientific_operation(connection, state)
        return True
    operation = await connection.fetchrow(
        """
        SELECT id,attempt
        FROM fs2_operations
        WHERE id=$1 AND protocol='scientific-batch-v1' AND status IN ('queued','running')
        FOR UPDATE
        """,
        operation_id,
    )
    if operation is None:
        return True
    if state.cancel_requested:
        return True
    replacement = replace(state, cancel_requested=True)
    updated = await connection.fetchval(
        """
        UPDATE fs2_scientific_batches
        SET cancel_requested=true,state=$2::jsonb,updated_at=clock_timestamp()
        WHERE operation_id=$1 AND status IN ('queued','running') AND NOT cancel_requested
        RETURNING operation_id
        """,
        operation_id,
        state_to_json(replacement),
    )
    if updated is None:
        return True
    await connection.execute(
        """
        INSERT INTO fs2_operation_events(operation_id,event,status,attempt)
        VALUES($1,$2,(SELECT status FROM fs2_operations WHERE id=$1),$3)
        """,
        operation_id,
        f"scientific_batch_{cause}_cancellation_requested",
        operation["attempt"],
    )
    return True


async def process_scientific_interruption_requests(
    connection: asyncpg.Connection[Any], *, limit: int = 16
) -> int:
    """Consume bounded maintenance handoffs under runtime/controller authority."""

    requests = await connection.fetch(
        """
        SELECT request.operation_id,request.cause
        FROM fs2_scientific_interruption_requests request
        JOIN fs2_operations operation ON operation.id=request.operation_id
        JOIN fs2_scientific_batches batch ON batch.operation_id=request.operation_id
        WHERE operation.protocol='scientific-batch-v1'
          AND (
            (operation.status IN ('queued','running') AND NOT batch.cancel_requested)
            OR (
              operation.status IN ('succeeded','failed','cancelled','preempted','expired')
              AND operation.payload_purged_at IS NULL
            )
          )
        ORDER BY
          (operation.status IN ('succeeded','failed','cancelled','preempted','expired')) DESC,
          request.requested_at,request.operation_id
        LIMIT $1
        """,
        max(1, min(limit, 100)),
    )
    processed = 0
    for request in requests:
        await account_interruption_and_request_cancel(
            connection,
            request["operation_id"],
            cause=request["cause"],
        )
        purged = await connection.fetchval(
            """
            UPDATE fs2_operations
            SET request_key_id=NULL,request_nonce=NULL,request_ciphertext=NULL,
                response_key_id=NULL,response_nonce=NULL,response_ciphertext=NULL,
                payload_purged_at=clock_timestamp()
            WHERE id=$1 AND payload_expires_at<=clock_timestamp()
              AND payload_purged_at IS NULL
              AND status IN ('succeeded','failed','cancelled','preempted','expired')
            RETURNING id
            """,
            request["operation_id"],
        )
        processed += int(purged is not None)
    return processed
