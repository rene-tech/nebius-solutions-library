"""Evidence-backed preferences, not reservations or a second GPU scheduler.

Rate snapshots and measurements live in the existing immutable PostgreSQL
benchmark registry. Missing prices or allocation time are unknown, never zero.
Only an explicitly placement-enabled, completed matched-input campaign can
affect placement; ordinary/advisory benchmarks cannot silently do so.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ComputeRate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usd_per_replica_hour: Decimal = Field(gt=0, le=100000, allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=1024, pattern=r"^(https|s3|artifact)://[^?]+$")
    observed_at: datetime
    allocation_basis: str = Field(min_length=1, max_length=512)

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("rate observation requires a timezone")
        return value


class PlacementTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=128)
    runtime_image: str = Field(pattern=r"^(?:[^@\s]+@)?sha256:[a-f0-9]{64}$", max_length=512)
    accelerators_per_replica: int = Field(ge=0, le=1024)
    workload_class: str = Field(min_length=1, max_length=128)
    cache_condition: Literal["cold", "warm", "snapshot"]


def cost_profiles(campaign: Mapping[str, Any]) -> list[dict[str, Any]]:
    """USD per semantic success, including occupied time of failed attempts.

    `allocated_compute_seconds` is elapsed allocation time of the *entire*
    priced replica (all GPUs/CPU/RAM), summed over attempts. It includes staging,
    loading, restore, export and attributed idle/cooldown, not just kernel time.
    Shared replicas must apportion the allocation once, not bill it per caller.
    The benchmark producer must measure this; elapsed request time is not a
    substitute, because it can include time waiting without an allocation.
    """
    target = campaign.get("spec", {}).get("placement_target")
    if target is None:
        return []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for trial in campaign.get("trials", []):
        pool = trial["case_spec"].get("requested_pool")
        if pool:
            groups.setdefault(pool, []).append(trial)
    profiles = []
    for pool, trials in groups.items():
        total = Decimal(0)
        successes = 0
        valid = True
        for trial in trials:
            case, result = trial["case_spec"], trial.get("result") or {}
            hardware, rate = result.get("hardware") or {}, case.get("compute_rate")
            seconds = result.get("allocated_compute_seconds")
            if (
                trial["status"] not in {"succeeded", "failed"}
                or seconds is None
                or rate is None
                or hardware.get("pool") != pool
                or hardware.get("runtime_image") != target["runtime_image"]
                or hardware.get("gpu_count") != target["accelerators_per_replica"]
                or case.get("model_id") != target["model_id"]
                or case.get("workload_class") != target["workload_class"]
                or case.get("cache_condition") != target["cache_condition"]
                or (trial["status"] == "succeeded" and not result.get("semantic_valid"))
            ):
                valid = False
                break
            total += Decimal(str(seconds)) * Decimal(str(rate["usd_per_replica_hour"]))
            successes += trial["status"] == "succeeded"
        if valid and successes >= 3:
            profiles.append(
                {
                    "pool": pool,
                    "usd_per_successful_request": str(total / (successes * Decimal(3600))),
                    "successes": successes,
                    "attempted_requests": len(trials),
                    "campaign_id": str(campaign["id"]),
                    "campaign_sha256": campaign["spec_sha256"],
                    "target": target,
                    "trial_ids": [str(t["id"]) for t in trials],
                    "rate": trials[0]["case_spec"]["compute_rate"],
                }
            )
    return sorted(profiles, key=lambda profile: Decimal(profile["usd_per_successful_request"]))


def ranked_pools(eligible: Sequence[str], profiles: Sequence[Mapping[str, Any]]) -> list[str]:
    costs = {p["pool"]: Decimal(p["usd_per_successful_request"]) for p in profiles}
    return sorted(eligible, key=lambda pool: (pool not in costs, costs.get(pool, Decimal(0)), eligible.index(pool)))
