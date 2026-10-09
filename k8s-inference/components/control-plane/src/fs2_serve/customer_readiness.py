"""Read the fail-closed output of the customer capability acceptance gate."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .models import StrictModel

VERDICT_SCHEMA = "fs2-serve.nebius.ai/customer-capability-verdict/v1"
INDEX_SCHEMA = "fs2-serve.nebius.ai/customer-capability-verdict-index/v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_SOURCE_REVISION = re.compile(r"[0-9a-f]{40}")
_VERDICT_FIELDS = {
    "schema",
    "app_id",
    "evaluated_at",
    "valid_until",
    "release_identity",
    "verdict",
    "ready",
    "required_capability_ids",
    "excluded_by_explicit_decision",
    "capabilities",
}
_IDENTITY_FIELDS = {
    "source_revision",
    "runtime_images",
    "configuration_sha256",
    "model_revision",
    "client_build_sha256",
    "tenant_policy_sha256",
    "public_endpoint",
    "public_tool_catalog_sha256",
}


class CustomerScenarioState(StrictModel):
    scenario_id: str
    state: Literal["untested", "partial", "failed", "qualified", "stale"]
    evidence_id: str | None = None
    reasons: list[str] = Field(default_factory=list)


class CustomerCapabilityState(StrictModel):
    capability_id: str
    advertised: bool
    requested: bool
    state: Literal["untested", "partial", "failed", "qualified", "stale"]
    required: bool
    scenarios: list[CustomerScenarioState] = Field(min_length=1)


class CustomerReadinessSummary(StrictModel):
    verdict: Literal["customer-ready", "not-ready"]
    ready: bool
    evaluated_at: AwareDatetime
    valid_until: AwareDatetime
    source_revision: str
    capabilities: list[CustomerCapabilityState] = Field(default_factory=list)

    @model_validator(mode="after")
    def verdict_matches_required_capabilities(self) -> CustomerReadinessSummary:
        if self.valid_until <= self.evaluated_at:
            raise ValueError("customer readiness validity must end after evaluation")
        if len({item.capability_id for item in self.capabilities}) != len(self.capabilities):
            raise ValueError("customer readiness capability identities must be unique")
        for capability in self.capabilities:
            states = {scenario.state for scenario in capability.scenarios}
            expected_state = (
                "qualified"
                if states == {"qualified"}
                else "failed"
                if "failed" in states
                else "stale"
                if "stale" in states
                else "untested"
                if states == {"untested"}
                else "partial"
            )
            if capability.state != expected_state:
                raise ValueError("customer readiness capability contradicts its scenario states")
        expected = bool([item for item in self.capabilities if item.required]) and all(
            item.state == "qualified" for item in self.capabilities if item.required
        )
        if self.ready != expected or self.verdict != ("customer-ready" if expected else "not-ready"):
            raise ValueError("customer readiness verdict contradicts required capability states")
        return self

    def effective(self, at: datetime | None = None) -> CustomerReadinessSummary:
        """Expire a cached positive verdict even when its projection is unchanged."""

        current = (at or datetime.now(UTC)).astimezone(UTC)
        if current <= self.valid_until:
            return self
        expired = self.model_copy(deep=True)
        expired.ready = False
        expired.verdict = "not-ready"
        for capability in expired.capabilities:
            for scenario in capability.scenarios:
                if scenario.state == "qualified":
                    scenario.state = "stale"
                    scenario.reasons = ["the published readiness evidence expired; rerun exact-release acceptance"]
            states = {scenario.state for scenario in capability.scenarios}
            capability.state = (
                "qualified"
                if states == {"qualified"}
                else "failed"
                if "failed" in states
                else "stale"
                if "stale" in states
                else "untested"
                if states == {"untested"}
                else "partial"
            )
        return expired


def load_customer_readiness(path: Path | None) -> dict[str, CustomerReadinessSummary]:
    """Load gate output by App/public model ID; missing evidence stays absent.

    The acceptance gate emits one verdict per invocation.  Operators may point
    the control plane straight at that file, or at a strictly shaped index when
    several Apps are evaluated independently.
    """

    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("customer readiness verdict index is unreadable") from error
    if not isinstance(raw, dict):
        raise ValueError("customer readiness verdict is invalid")
    if raw.get("schema") == VERDICT_SCHEMA:
        verdicts = [raw]
    elif set(raw) == {"schema", "verdicts"} and raw.get("schema") == INDEX_SCHEMA:
        verdicts = raw["verdicts"]
        if not isinstance(verdicts, list):
            raise ValueError("customer readiness verdicts must be a list")
    else:
        raise ValueError("customer readiness verdict index is invalid")
    indexed: dict[str, CustomerReadinessSummary] = {}
    for verdict in verdicts:
        if not isinstance(verdict, dict) or set(verdict) != _VERDICT_FIELDS or verdict.get("schema") != VERDICT_SCHEMA:
            raise ValueError("customer readiness verdict schema is invalid")
        app_id = verdict.get("app_id")
        identity = verdict.get("release_identity")
        if not isinstance(app_id, str) or not app_id or app_id in indexed or not isinstance(identity, dict):
            raise ValueError("customer readiness App identity is invalid")
        if set(identity) != _IDENTITY_FIELDS:
            raise ValueError("customer readiness release identity is invalid")
        source_revision = identity.get("source_revision")
        images = identity.get("runtime_images")
        digests = (
            identity.get("configuration_sha256"),
            identity.get("client_build_sha256"),
            identity.get("tenant_policy_sha256"),
            identity.get("public_tool_catalog_sha256"),
        )
        if (
            not isinstance(source_revision, str)
            or _SOURCE_REVISION.fullmatch(source_revision) is None
            or not isinstance(images, dict)
            or not images
            or any(not isinstance(key, str) or not key for key in images)
            or any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in images.values())
            or any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in digests)
            or not isinstance(identity.get("model_revision"), str)
            or not identity["model_revision"]
            or not isinstance(identity.get("public_endpoint"), str)
            or not identity["public_endpoint"].startswith("https://")
        ):
            raise ValueError("customer readiness release identity is invalid")
        rows = verdict.get("capabilities")
        if not isinstance(rows, list):
            raise ValueError("customer readiness capability rows are invalid")
        required = verdict.get("required_capability_ids")
        excluded = verdict.get("excluded_by_explicit_decision")
        if (
            not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or required != sorted(set(required))
            or not isinstance(excluded, list)
            or any(not isinstance(item, str) for item in excluded)
            or excluded != sorted(set(excluded))
            or any(not isinstance(row, dict) for row in rows)
            or set(required)
            != {
                row.get("capability_id")
                for row in rows
                if isinstance(row, dict) and row.get("required") and isinstance(row.get("capability_id"), str)
            }
        ):
            raise ValueError("customer readiness required capability identity is invalid")
        indexed[app_id] = CustomerReadinessSummary.model_validate(
            {
                "verdict": verdict.get("verdict"),
                "ready": verdict.get("ready"),
                "evaluated_at": verdict.get("evaluated_at"),
                "valid_until": verdict.get("valid_until"),
                "source_revision": source_revision,
                "capabilities": [row for row in rows if isinstance(row, dict)],
            }
        )
    return indexed


class CustomerReadinessFile:
    """Reload an atomically projected verdict index without restarting the API."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._identity: tuple[int, int, int] | None = None
        self._value: dict[str, CustomerReadinessSummary] = {}

    def read(self) -> dict[str, CustomerReadinessSummary]:
        if self.path is None or not self.path.is_file():
            self._identity = None
            self._value = {}
            return {}
        stat = self.path.stat()
        identity = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if identity != self._identity:
            value = load_customer_readiness(self.path)
            self._value = value
            self._identity = identity
        return {app_id: verdict.effective() for app_id, verdict in self._value.items()}
