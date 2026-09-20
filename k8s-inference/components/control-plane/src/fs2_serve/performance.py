"""Durable benchmark campaigns; independent of admission and replica ownership.

PostgreSQL owns benchmark intent and results. Existing public operation APIs own
execution. Workers reuse the trial UUID as their inference idempotency key, so a
lease recovery resumes the same operation rather than submitting new GPU work.
No measurement here silently changes a ModelDeployment or a Kueue reservation.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .store import ConflictError, NotFoundError

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkCase(Contract):
    case_id: Identifier
    model_id: Identifier
    workload_class: Identifier
    fixture_sha256: Digest
    adapter: Identifier
    fixture_ref: str = Field(min_length=1, max_length=512)
    repetitions: int = Field(default=3, ge=1, le=30)
    requested_pool: Identifier | None = None
    cache_condition: Literal["uncontrolled", "warm", "cold", "snapshot"] = "uncontrolled"
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=512)


class CampaignCreate(Contract):
    name: Identifier
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    catalog_sha256: Digest
    max_parallel: int = Field(default=2, ge=1, le=16)
    cases: list[BenchmarkCase] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def distinct(self) -> CampaignCreate:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        if sum(case.repetitions for case in self.cases) > 4096:
            raise ValueError("a campaign may contain at most 4096 trials")
        return self


class ClaimRequest(Contract):
    worker: Identifier
    lease_seconds: int = Field(default=120, ge=30, le=600)


class TrialLease(Contract):
    worker: Identifier
    fence: int = Field(ge=1)


class HardwareObservation(Contract):
    """Actual execution environment, never inferred from a requested pool."""

    pool: Identifier
    gpu_product: str = Field(min_length=1, max_length=128)
    gpu_count: int = Field(ge=0, le=1024)
    gpus_per_node: int = Field(ge=0, le=1024)
    cpu_arch: str = Field(min_length=1, max_length=32)
    driver_version: str = Field(min_length=1, max_length=64)
    runtime_image: str = Field(pattern=r"^.+@sha256:[a-f0-9]{64}$", max_length=512)
    runtime_fingerprint: Digest
    topology: Literal["single-device", "single-node", "multi-node"]
    local_storage: Literal["present", "absent", "unknown"]


class TrialResult(TrialLease):
    status: Literal["succeeded", "failed", "capacity-unavailable", "unsupported"]
    operation_id: UUID | None = None
    semantic_valid: bool = False
    error_code: Identifier | None = None
    elapsed_seconds: Seconds | None = None
    queue_seconds: Seconds | None = None
    startup_seconds: Seconds | None = None
    execution_seconds: Seconds | None = None
    gpu_occupied_seconds: Seconds | None = None
    hardware: HardwareObservation | None = None
    receipt_sha256: Digest
    artifact_uri: str = Field(min_length=1, max_length=1024, pattern=r"^(s3|artifact)://[^?]+$")

    @model_validator(mode="after")
    def valid_success(self) -> TrialResult:
        if self.status == "succeeded" and (not self.semantic_valid or self.operation_id is None):
            raise ValueError("success requires a public operation and semantic validation")
        if self.status != "succeeded" and self.error_code is None:
            raise ValueError("unsuccessful trials require an explicit reason")
        return self


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode(row: Any) -> dict[str, Any]:
    value = dict(row)
    for field in ("spec", "result", "case_spec"):
        if isinstance(value.get(field), str):
            value[field] = json.loads(value[field])
    return value


class PerformanceRepository:
    def __init__(self, pool: Any):
        self.pool = pool

    async def create(self, spec: CampaignCreate, actor: str) -> dict[str, Any]:
        payload = canonical(spec.model_dump(mode="json"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,35))", spec.name)
            prior = await conn.fetchrow("SELECT * FROM fs2_benchmark_campaigns WHERE name=$1", spec.name)
            if prior:
                if prior["spec_sha256"] != digest:
                    raise ConflictError("campaign name already exists with different immutable inputs")
                return decode(prior)
            campaign = await conn.fetchrow(
                """INSERT INTO fs2_benchmark_campaigns(id,name,spec,spec_sha256,created_by,max_parallel)
                   VALUES($1,$2,$3::jsonb,$4,$5,$6) RETURNING *""",
                uuid4(),
                spec.name,
                payload,
                digest,
                actor,
                spec.max_parallel,
            )
            for case in spec.cases:
                for repetition in range(1, case.repetitions + 1):
                    await conn.execute(
                        """INSERT INTO fs2_benchmark_trials
                           (id,campaign_id,case_id,model_id,workload_class,repetition,case_spec,status)
                           VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)""",
                        uuid4(),
                        campaign["id"],
                        case.case_id,
                        case.model_id,
                        case.workload_class,
                        repetition,
                        canonical(case.model_dump(mode="json")),
                        "unsupported" if case.unavailable_reason else "queued",
                    )
            return decode(campaign)

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """SELECT c.id,c.name,c.created_at,c.max_parallel,c.spec_sha256,
                      c.spec->>'source_commit' AS source_commit,
                      count(t.id) AS total,
                      count(t.id) FILTER(WHERE t.status='queued') AS queued,
                      count(t.id) FILTER(WHERE t.status='running') AS running,
                      count(t.id) FILTER(WHERE t.status='succeeded') AS succeeded,
                      count(t.id) FILTER(WHERE t.status='failed') AS failed,
                      count(t.id) FILTER(WHERE t.status='unsupported') AS unsupported,
                      count(t.id) FILTER(WHERE t.status='capacity-unavailable') AS capacity_unavailable
               FROM fs2_benchmark_campaigns c LEFT JOIN fs2_benchmark_trials t ON t.campaign_id=c.id
               GROUP BY c.id ORDER BY c.created_at DESC,c.id LIMIT $1""",
            limit,
        )
        return [dict(row) for row in rows]

    async def detail(self, campaign_id: UUID) -> dict[str, Any]:
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            row = await conn.fetchrow("SELECT * FROM fs2_benchmark_campaigns WHERE id=$1", campaign_id)
            if row is None:
                raise NotFoundError("benchmark campaign was not found")
            trials = await conn.fetch(
                """SELECT * FROM fs2_benchmark_trials WHERE campaign_id=$1
                   ORDER BY model_id,case_id,repetition""",
                campaign_id,
            )
        return {**decode(row), "trials": [decode(trial) for trial in trials]}

    async def claim(self, campaign_id: UUID, request: ClaimRequest) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn, conn.transaction():
            # Serialize admission only per campaign, not globally or during model execution.
            campaign = await conn.fetchrow(
                "SELECT max_parallel FROM fs2_benchmark_campaigns WHERE id=$1 FOR UPDATE",
                campaign_id,
            )
            if campaign is None:
                raise NotFoundError("benchmark campaign was not found")
            active = await conn.fetchval(
                """SELECT count(*) FROM fs2_benchmark_trials WHERE campaign_id=$1
                   AND status='running' AND lease_until>clock_timestamp()""",
                campaign_id,
            )
            if active >= campaign["max_parallel"]:
                return None
            row = await conn.fetchrow(
                """SELECT * FROM fs2_benchmark_trials WHERE campaign_id=$1 AND
                   (status='queued' OR (status='running' AND lease_until<=clock_timestamp()))
                   ORDER BY created_at,case_id,repetition FOR UPDATE SKIP LOCKED LIMIT 1""",
                campaign_id,
            )
            if row is None:
                return None
            await conn.execute(
                """UPDATE fs2_benchmark_attempts SET finished_at=clock_timestamp(),outcome='lease-expired'
                   WHERE trial_id=$1 AND finished_at IS NULL""",
                row["id"],
            )
            claimed = await conn.fetchrow(
                """UPDATE fs2_benchmark_trials SET status='running',worker=$2,fence=fence+1,
                   lease_until=clock_timestamp()+make_interval(secs=>$3),updated_at=clock_timestamp()
                   WHERE id=$1 RETURNING *""",
                row["id"],
                request.worker,
                request.lease_seconds,
            )
            await conn.execute(
                """INSERT INTO fs2_benchmark_attempts(trial_id,fence,worker)
                   VALUES($1,$2,$3)""",
                row["id"],
                claimed["fence"],
                request.worker,
            )
            return decode(claimed)

    async def heartbeat(self, trial_id: UUID, lease: TrialLease) -> None:
        changed = await self.pool.fetchval(
            """UPDATE fs2_benchmark_trials SET lease_until=clock_timestamp()+interval '120 seconds'
               WHERE id=$1 AND worker=$2 AND fence=$3 AND status='running'
               AND lease_until>clock_timestamp() RETURNING id""",
            trial_id,
            lease.worker,
            lease.fence,
        )
        if changed is None:
            raise ConflictError("benchmark lease expired or was superseded")

    async def finish(self, trial_id: UUID, result: TrialResult) -> None:
        payload = canonical(result.model_dump(mode="json"))
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM fs2_benchmark_trials WHERE id=$1 FOR UPDATE", trial_id)
            if row is None:
                raise NotFoundError("benchmark trial was not found")
            if row["fence"] == result.fence and row["worker"] == result.worker and row["result"]:
                if canonical(decode(row)["result"]) == payload:
                    return
                raise ConflictError("benchmark result is immutable")
            if (
                row["status"] != "running"
                or row["worker"] != result.worker
                or row["fence"] != result.fence
                or row["lease_until"] <= datetime.now(UTC)
            ):
                raise ConflictError("benchmark lease expired or was superseded")
            await conn.execute(
                """UPDATE fs2_benchmark_trials SET status=$2,result=$3::jsonb,lease_until=NULL,
                   updated_at=clock_timestamp() WHERE id=$1""",
                trial_id,
                result.status,
                payload,
            )
            await conn.execute(
                """UPDATE fs2_benchmark_attempts SET finished_at=clock_timestamp(),outcome=$3
                   WHERE trial_id=$1 AND fence=$2""",
                trial_id,
                result.fence,
                result.status,
            )


def summarize_profiles(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never mix runtime images, input sizes, hardware, or cold/warm conditions.

    Three repetitions are a baseline, not a robust tail-latency estimate. Failed
    samples remain in the denominator. Cross-GPU recommendations require actual
    hardware observations and comparable valid cohorts; unknown is not zero.
    """
    groups: dict[str, dict[str, Any]] = {}
    for trial in trials:
        case, result = trial["case_spec"], trial.get("result") or {}
        identity = {key: case.get(key) for key in ("model_id", "workload_class", "fixture_sha256", "cache_condition")}
        identity["hardware"] = result.get("hardware")
        key = canonical(identity)
        group = groups.setdefault(key, {**identity, "samples": [], "outcomes": {}})
        group["outcomes"][trial["status"]] = group["outcomes"].get(trial["status"], 0) + 1
        if trial["status"] == "succeeded" and result.get("semantic_valid"):
            group["samples"].append(result)
    profiles = []
    for group in groups.values():
        samples = group.pop("samples")
        metrics: dict[str, dict[str, Any]] = {}
        for metric in (
            "elapsed_seconds",
            "queue_seconds",
            "startup_seconds",
            "execution_seconds",
            "gpu_occupied_seconds",
        ):
            values = [sample[metric] for sample in samples if sample.get(metric) is not None]
            metrics[metric] = {
                "count": len(values),
                "median": statistics.median(values) if values else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
            }
        qualified = (
            group["hardware"] is not None
            and len(samples) >= 3
            and sum(group["outcomes"].values()) == len(samples)
            and metrics["execution_seconds"]["count"] >= 3
        )
        profiles.append(
            {
                **group,
                "valid_samples": len(samples),
                "metrics": metrics,
                "placement_evidence": "baseline" if qualified else "insufficient",
                "mode": "advisory",
                "automatic_placement": False,
            }
        )
    return profiles
