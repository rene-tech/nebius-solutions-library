"""Archive-backed retirement of a drained serving App.

Keep revisions, usage, audit and identity tombstones. Stop projecting the retired
desired state and let the existing controller bridge remove its CR, whose owned
workloads are garbage-collected by Kubernetes. Weight PVC cleanup is separate.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .model_deployment import DesiredState
from .models import StrictModel
from .store import ConflictError, NotFoundError


class ModelRetirementRequest(StrictModel):
    expected_etag: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


async def retire_model(service: Any, name: str, request: ModelRetirementRequest, actor: str) -> dict[str, Any]:
    from .postgres import PostgresStore

    pool = getattr(service.repository.store, "pool", None)
    if pool is None:
        raise ValueError("App retirement requires PostgreSQL")
    namespace = service.namespace
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL lock_timeout = '5s'")
        await PostgresStore._model_deployment_lock(connection, namespace, name)
        current = await connection.fetchrow(
            "SELECT * FROM fs2_model_deployments WHERE namespace=$1 AND name=$2 FOR UPDATE",
            namespace,
            name,
        )
        if current is None:
            raise NotFoundError("model deployment was not found")
        if current["current_etag"] != request.expected_etag:
            raise ConflictError("model deployment changed since archival")
        if current["retired_at"] is not None:
            if current["retirement_archive_sha256"] != request.archive_sha256:
                raise ConflictError("model already retired with a different archive")
            return {
                "namespace": namespace,
                "name": name,
                "retired_at": current["retired_at"],
                "archive_sha256": request.archive_sha256,
                "history_retained": True,
                "resource_cleanup": "controller_pending",
            }
        raw = await connection.fetchrow(
            "SELECT * FROM fs2_model_deployment_revisions WHERE namespace=$1 AND name=$2 AND revision=$3",
            namespace,
            name,
            current["current_revision"],
        )
        revision = PostgresStore._model_deployment_revision(raw)
        if revision.spec.lifecycle.desired_state is DesiredState.ENABLED:
            raise ConflictError("drain the model and wait for zero replicas before retirement")
        active = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM fs2_operations WHERE model_id=$1
            AND status NOT IN ('succeeded','failed','cancelled','preempted','expired'))""",
            revision.spec.public_model_id,
        )
        if active:
            raise ConflictError("model still has active operations")
        # Fresh Kubernetes observations, not an assumed zero from missing data.
        await service.writer.check_retirement(revision)
        row = await connection.fetchrow(
            """UPDATE fs2_model_deployments SET retired_at=clock_timestamp(),retired_by=$3,
            retirement_archive_sha256=$4 WHERE namespace=$1 AND name=$2 RETURNING retired_at""",
            namespace,
            name,
            actor,
            request.archive_sha256,
        )
        await PostgresStore._audit(
            connection,
            actor=actor,
            tenant_id=revision.tenant_id,
            token_id=None,
            action="model_deployment.retire",
            target_type="model_deployment",
            target_id=namespace + "/" + name,
            outcome="succeeded",
            detail={
                "revision": revision.revision,
                "etag": revision.etag,
                "archive_sha256": request.archive_sha256,
                "history_retained": True,
            },
        )
        return {
            "namespace": namespace,
            "name": name,
            "retired_at": row["retired_at"],
            "archive_sha256": request.archive_sha256,
            "history_retained": True,
            "resource_cleanup": "controller_pending",
        }
