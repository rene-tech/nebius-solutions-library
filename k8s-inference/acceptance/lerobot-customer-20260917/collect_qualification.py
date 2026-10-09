#!/usr/bin/env python3
"""Read one disposable LeRobot parent's accepted scheduler and child identities.

No admissions, retries, cancellation, policy changes or SQL writes. The existing
CP database credential stays inside its Pod. Only exact-parent, exact-principal,
robotics-tenant identity/status/scheduler columns are selected; request/response
payloads and capability/environment values are never selected. Output is private.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from release_operator import check, kube, write_private

MODEL = "cosmos3-lerobot-augmentation"
PREFIX = "robotics-lerobot-canary-"

# Parameters are encoded as JSON literals below, not interpolated into SQL.
POD_READ = r'''
import asyncio, json, os
from datetime import datetime, timezone
from uuid import UUID
import asyncpg
from fs2_serve.settings import Settings

async def capture():
    settings = Settings()
    connection = await asyncpg.connect(
        settings.database_url.replace('postgresql+asyncpg://', 'postgresql://', 1),
        timeout=15, command_timeout=15,
    )
    try:
        async with connection.transaction(readonly=True, isolation='repeatable_read'):
            parent = await connection.fetchrow("""
                SELECT o.id,o.model_id,o.model_revision,o.protocol,o.operation,o.status,
                       o.tenant_id,o.principal_id,o.accepted_at,o.started_at,o.completed_at,
                       o.error_code,o.attempt,t.max_concurrency,
                       b.batch_id,b.workload_id,b.variant_id,b.status AS batch_status,
                       b.revision,b.scheduling_digest,b.state->'scheduling' AS scheduling,
                       b.state->'stages' AS stages,
                       b.state->'adapter_execution'->>'execution_map_sha256' AS execution_map_sha256,
                       b.state->'adapter_execution'->>'request_sha256' AS request_sha256,
                       (SELECT jsonb_agg(jsonb_build_object(
                           'stage_id',s->'stage_id','image',s->'image',
                           'model_runtime_image_digest',s->'model_runtime_image_digest',
                           'collector_id',s->'collector_id','validator_id',s->'validator_id',
                           'request_cpu',s->'request_cpu','request_memory',s->'request_memory',
                           'request_ephemeral_storage',s->'request_ephemeral_storage',
                           'limit_cpu',s->'limit_cpu','limit_memory',s->'limit_memory',
                           'limit_ephemeral_storage',s->'limit_ephemeral_storage'
                        )) FROM jsonb_array_elements(b.state->'adapter_execution'->'stage_bindings') s)
                       AS stage_bindings
                FROM fs2_operations o
                JOIN fs2_scientific_batches b ON b.operation_id=o.id AND b.tenant_id=o.tenant_id
                JOIN fs2_tokens t ON t.id=o.token_id
                WHERE o.id=$1 AND o.principal_id=$2 AND o.tenant_id='robotics'
                  AND o.model_id='cosmos3-lerobot-augmentation'
            """, UUID(PARENT_ID), PRINCIPAL)
            if parent is None:
                raise RuntimeError('exact_disposable_parent_not_found')
            children = await connection.fetch("""
                SELECT c.id,c.parent_operation_id,c.parent_attempt_id,c.model_id,c.model_revision,
                       c.protocol,c.operation,c.status,c.semantic_outcome,c.http_status,c.error_code,
                       c.accepted_at,c.activation_started_at,c.ready_at,c.started_at,c.completed_at,
                       c.attempt,c.pod_uid,c.node_uid,c.gpu_uuids,c.gpu_count,c.preemptible,
                       c.cold_start_seconds,c.reserved_gpu_seconds,
                       c.token_id=p.token_id AS same_token,
                       c.principal_id=p.principal_id AS same_principal,
                       c.tenant_id=p.tenant_id AS same_tenant
                FROM fs2_operations c JOIN fs2_operations p ON p.id=c.parent_operation_id
                WHERE p.id=$1 AND p.principal_id=$2 AND p.tenant_id='robotics'
                  AND p.model_id='cosmos3-lerobot-augmentation'
                ORDER BY c.accepted_at,c.id LIMIT 4097
            """, UUID(PARENT_ID), PRINCIPAL)
            if len(children)>4096:
                raise RuntimeError('child_evidence_bound_exceeded')
            value=dict(parent)
            for key in ('scheduling','stages','stage_bindings'):
                if isinstance(value[key],str):
                    value[key]=json.loads(value[key])
            print(json.dumps({
                'schema':'fs2-serve.nebius.ai/lerobot-parent-child-observation/v1',
                'captured_at':datetime.now(timezone.utc).isoformat(),
                'source_control_plane_pod':os.environ.get('HOSTNAME'),
                'database_transaction_read_only':True,
                'scope':'exact disposable parent and its children; no request/response payloads',
                'parent':value,'children':[dict(row) for row in children],
            },default=str,sort_keys=True))
    finally:
        await connection.close()

asyncio.run(capture())
'''


def make_script(parent_id: str, principal: str) -> str:
    parent_id = str(UUID(parent_id))
    check(principal.startswith(PREFIX), "disposable_principal_required")
    UUID(principal.removeprefix(PREFIX))
    return (
        f"PARENT_ID={json.dumps(parent_id)}\nPRINCIPAL={json.dumps(principal)}\n"
        + POD_READ
    )


def validate(value: dict, parent_id: str, principal: str) -> None:
    parent = value["parent"]
    check(parent["id"] == str(UUID(parent_id)), "parent_identity_mismatch")
    check(
        parent["principal_id"] == principal and parent["tenant_id"] == "robotics",
        "parent_owner_mismatch",
    )
    check(parent["model_id"] == MODEL, "parent_model_mismatch")
    check(
        value["database_transaction_read_only"] is True, "readonly_transaction_required"
    )
    attempts = {
        attempt["attempt_id"]
        for stage in parent["stages"]
        for attempt in stage["attempts"]
    }
    for child in value["children"]:
        check(child["parent_operation_id"] == parent["id"], "child_parent_mismatch")
        check(child["parent_attempt_id"] in attempts, "child_attempt_mismatch")
        check(
            child["same_token"] and child["same_principal"] and child["same_tenant"],
            "child_policy_owner_mismatch",
        )
        check(child["model_id"] == "cosmos3-nano", "unexpected_child_model")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    script = make_script(args.parent_id, args.principal_id)
    check(not args.output.exists(), "receipt_exists")
    value = kube(
        args.kubeconfig,
        args.context,
        "-n",
        "fs2-system",
        "exec",
        "-i",
        "deploy/fs2-serve-control-plane",
        "-c",
        "control-plane",
        "--",
        "python",
        "-",
        script=script,
    )
    validate(value, args.parent_id, args.principal_id)
    write_private(args.output, value)
    print(
        json.dumps(
            {
                "parent_id": args.parent_id,
                "parent_status": value["parent"]["status"],
                "children": len(value["children"]),
                "receipt": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
