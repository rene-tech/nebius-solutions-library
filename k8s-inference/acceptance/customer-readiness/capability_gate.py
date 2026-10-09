#!/usr/bin/env python3
"""Fail-closed customer capability evidence gate.

The evaluator deliberately knows nothing about model families. A manifest names
the exact customer scenarios that are advertised or requested; evidence can
qualify only the same scenario on the same immutable release identity.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "fs2-serve.nebius.ai/customer-capability-manifest/v1"
EVIDENCE_SCHEMA = "fs2-serve.nebius.ai/customer-capability-evidence/v1"
VERDICT_SCHEMA = "fs2-serve.nebius.ai/customer-capability-verdict/v1"
STATES = frozenset({"untested", "partial", "failed", "qualified", "stale"})
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_SOURCE_REVISION = re.compile(r"[0-9a-f]{40}")
_IDENTITY_KEYS = {
    "source_revision",
    "runtime_images",
    "configuration_sha256",
    "model_revision",
    "client_build_sha256",
    "tenant_policy_sha256",
    "public_endpoint",
    "public_tool_catalog_sha256",
}
_SCENARIO_KEYS = {
    "id",
    "operation",
    "input_form",
    "output_form",
    "client_path",
    "workload_states",
    "integrations",
    "required_observability",
    "min_clean_cohorts",
}
_EVIDENCE_KEYS = {
    "schema",
    "evidence_id",
    "app_id",
    "capability_id",
    "scenario_id",
    "operation",
    "input_form",
    "output_form",
    "client_path",
    "observed_at",
    "release_identity",
    "fixture_sha256",
    "outcome",
    "clean_cohorts",
    "public_endpoint_exercised",
    "direct_runtime_only",
    "terminal_outcome_validated",
    "artifact_validated",
    "tenant_policy_exercised",
    "workload_states",
    "integrations",
    "observed_surfaces",
    "notes",
}


class CapabilityGateError(ValueError):
    pass


def _exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CapabilityGateError(f"{label} fields are invalid")
    return value


def _text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or not value.isprintable()
    ):
        raise CapabilityGateError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    text = _text(value, label)
    if _SHA256.fullmatch(text) is None:
        raise CapabilityGateError(f"{label} must be an exact SHA-256")
    return text


def _timestamp(value: object, label: str) -> datetime:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise CapabilityGateError(f"{label} is not RFC3339") from error
    if parsed.tzinfo is None:
        raise CapabilityGateError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _strings(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise CapabilityGateError(
            f"{label} must be a {'possibly empty ' if allow_empty else ''}list"
        )
    result = [_text(item, label) for item in value]
    if result != sorted(set(result)):
        raise CapabilityGateError(f"{label} must be sorted and unique")
    return result


def validate_identity(value: object) -> dict[str, Any]:
    identity = _exact(value, _IDENTITY_KEYS, "release identity")
    source = _text(identity["source_revision"], "source revision")
    if _SOURCE_REVISION.fullmatch(source) is None:
        raise CapabilityGateError(
            "source revision must be an exact 40-character Git revision"
        )
    images = identity["runtime_images"]
    if not isinstance(images, dict) or not images:
        raise CapabilityGateError("runtime_images must be a non-empty object")
    normalized_images = {
        _text(name, "runtime image role"): _digest(digest, "runtime image digest")
        for name, digest in sorted(images.items())
    }
    endpoint = _text(identity["public_endpoint"], "public endpoint")
    if not endpoint.startswith("https://"):
        raise CapabilityGateError("public endpoint must use HTTPS")
    return {
        "source_revision": source,
        "runtime_images": normalized_images,
        "configuration_sha256": _digest(
            identity["configuration_sha256"], "configuration digest"
        ),
        "model_revision": _text(identity["model_revision"], "model revision"),
        "client_build_sha256": _digest(
            identity["client_build_sha256"], "client build digest"
        ),
        "tenant_policy_sha256": _digest(
            identity["tenant_policy_sha256"], "tenant policy digest"
        ),
        "public_endpoint": endpoint.rstrip("/"),
        "public_tool_catalog_sha256": _digest(
            identity["public_tool_catalog_sha256"], "public tool catalog digest"
        ),
    }


def validate_manifest(value: object) -> dict[str, Any]:
    manifest = _exact(
        value,
        {
            "schema",
            "app_id",
            "release_identity",
            "max_evidence_age_seconds",
            "capabilities",
            "requested_capability_ids",
            "reduced_scope_decisions",
        },
        "capability manifest",
    )
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise CapabilityGateError("capability manifest schema is unsupported")
    max_age = manifest["max_evidence_age_seconds"]
    if (
        not isinstance(max_age, int)
        or isinstance(max_age, bool)
        or not 60 <= max_age <= 31_536_000
    ):
        raise CapabilityGateError(
            "max_evidence_age_seconds is outside the supported bound"
        )
    capabilities = manifest["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise CapabilityGateError("capabilities must be a non-empty list")
    normalized_capabilities = []
    capability_ids: set[str] = set()
    for raw_capability in capabilities:
        capability = _exact(
            raw_capability,
            {"id", "description", "advertised", "scenarios"},
            "capability",
        )
        capability_id = _text(capability["id"], "capability ID")
        if capability_id in capability_ids or not isinstance(
            capability["advertised"], bool
        ):
            raise CapabilityGateError(
                "capability IDs must be unique and advertised must be boolean"
            )
        capability_ids.add(capability_id)
        scenarios = capability["scenarios"]
        if not isinstance(scenarios, list) or not scenarios:
            raise CapabilityGateError(f"{capability_id}: scenarios must be non-empty")
        normalized_scenarios = []
        scenario_ids: set[str] = set()
        for raw_scenario in scenarios:
            scenario = _exact(raw_scenario, _SCENARIO_KEYS, f"{capability_id} scenario")
            scenario_id = _text(scenario["id"], "scenario ID")
            cohorts = scenario["min_clean_cohorts"]
            if (
                scenario_id in scenario_ids
                or not isinstance(cohorts, int)
                or isinstance(cohorts, bool)
                or cohorts < 1
            ):
                raise CapabilityGateError(
                    f"{capability_id}: scenario identity or cohort bound is invalid"
                )
            scenario_ids.add(scenario_id)
            normalized_scenarios.append(
                {
                    "id": scenario_id,
                    "operation": _text(scenario["operation"], "operation"),
                    "input_form": _text(scenario["input_form"], "input form"),
                    "output_form": _text(scenario["output_form"], "output form"),
                    "client_path": _text(scenario["client_path"], "client path"),
                    "workload_states": _strings(
                        scenario["workload_states"], "workload states"
                    ),
                    "integrations": _strings(
                        scenario["integrations"], "integrations", allow_empty=True
                    ),
                    "required_observability": _strings(
                        scenario["required_observability"], "required observability"
                    ),
                    "min_clean_cohorts": cohorts,
                }
            )
        normalized_capabilities.append(
            {
                "id": capability_id,
                "description": _text(
                    capability["description"], "capability description"
                ),
                "advertised": capability["advertised"],
                "scenarios": normalized_scenarios,
            }
        )
    requested = _strings(
        manifest["requested_capability_ids"],
        "requested capability IDs",
        allow_empty=True,
    )
    unknown = set(requested) - capability_ids
    if unknown:
        raise CapabilityGateError(
            f"requested capabilities are absent from the manifest: {sorted(unknown)}"
        )
    decisions = manifest["reduced_scope_decisions"]
    if not isinstance(decisions, list):
        raise CapabilityGateError("reduced_scope_decisions must be a list")
    normalized_decisions = []
    decided: set[str] = set()
    for raw_decision in decisions:
        decision = _exact(
            raw_decision,
            {"capability_id", "decision_id", "decided_by", "decided_at", "rationale"},
            "reduced-scope decision",
        )
        capability_id = _text(decision["capability_id"], "reduced capability ID")
        if capability_id not in requested or capability_id in decided:
            raise CapabilityGateError(
                "reduced-scope decisions must uniquely reference a requested capability"
            )
        decided.add(capability_id)
        normalized_decisions.append(
            {
                "capability_id": capability_id,
                "decision_id": _text(decision["decision_id"], "decision ID"),
                "decided_by": _text(decision["decided_by"], "decision owner"),
                "decided_at": _timestamp(
                    decision["decided_at"], "decision timestamp"
                ).isoformat(),
                "rationale": _text(decision["rationale"], "decision rationale"),
            }
        )
    return {
        "schema": MANIFEST_SCHEMA,
        "app_id": _text(manifest["app_id"], "App ID"),
        "release_identity": validate_identity(manifest["release_identity"]),
        "max_evidence_age_seconds": max_age,
        "capabilities": normalized_capabilities,
        "requested_capability_ids": requested,
        "reduced_scope_decisions": normalized_decisions,
    }


def validate_evidence(value: object) -> dict[str, Any]:
    evidence = _exact(value, _EVIDENCE_KEYS, "capability evidence")
    if evidence["schema"] != EVIDENCE_SCHEMA:
        raise CapabilityGateError("capability evidence schema is unsupported")
    outcome = evidence["outcome"]
    cohorts = evidence["clean_cohorts"]
    if outcome not in {"passed", "failed", "skipped"}:
        raise CapabilityGateError("evidence outcome is invalid")
    if not isinstance(cohorts, int) or isinstance(cohorts, bool) or cohorts < 0:
        raise CapabilityGateError("clean_cohorts is invalid")
    booleans = (
        "public_endpoint_exercised",
        "direct_runtime_only",
        "terminal_outcome_validated",
        "artifact_validated",
        "tenant_policy_exercised",
    )
    if any(not isinstance(evidence[field], bool) for field in booleans):
        raise CapabilityGateError("evidence validation flags must be boolean")
    notes = evidence["notes"]
    if not isinstance(notes, list) or any(not isinstance(item, str) for item in notes):
        raise CapabilityGateError("evidence notes must be strings")
    return {
        **evidence,
        "evidence_id": _text(evidence["evidence_id"], "evidence ID"),
        "app_id": _text(evidence["app_id"], "evidence App ID"),
        "capability_id": _text(evidence["capability_id"], "evidence capability ID"),
        "scenario_id": _text(evidence["scenario_id"], "evidence scenario ID"),
        "operation": _text(evidence["operation"], "evidence operation"),
        "input_form": _text(evidence["input_form"], "evidence input form"),
        "output_form": _text(evidence["output_form"], "evidence output form"),
        "client_path": _text(evidence["client_path"], "evidence client path"),
        "observed_at": _timestamp(
            evidence["observed_at"], "evidence timestamp"
        ).isoformat(),
        "release_identity": validate_identity(evidence["release_identity"]),
        "fixture_sha256": _digest(evidence["fixture_sha256"], "fixture digest"),
        "workload_states": _strings(
            evidence["workload_states"], "evidence workload states"
        ),
        "integrations": _strings(
            evidence["integrations"], "evidence integrations", allow_empty=True
        ),
        "observed_surfaces": _strings(
            evidence["observed_surfaces"], "evidence observed surfaces"
        ),
    }


def _scenario_state(
    scenario: dict[str, Any],
    receipts: list[dict[str, Any]],
    identity: dict[str, Any],
    stale_before: datetime,
    evidence_lifetime: timedelta,
) -> tuple[str, list[str], str | None, datetime | None]:
    if not receipts:
        return "untested", ["no evidence exists for the exact scenario"], None, None
    current = [item for item in receipts if item["release_identity"] == identity]
    if not current:
        return (
            "stale",
            ["all evidence belongs to another release identity"],
            receipts[-1]["evidence_id"],
            None,
        )
    # Equal-time receipts can occur in one batched acceptance publication. A
    # passing file name/order must never mask a failure at the same instant.
    outcome_precedence = {"passed": 0, "skipped": 1, "failed": 2}
    latest = max(
        current,
        key=lambda item: (
            _timestamp(item["observed_at"], "evidence timestamp"),
            outcome_precedence[item["outcome"]],
        ),
    )
    observed_at = _timestamp(latest["observed_at"], "evidence timestamp")
    if observed_at <= stale_before:
        return (
            "stale",
            ["the newest exact-release evidence exceeded max_evidence_age_seconds"],
            latest["evidence_id"],
            None,
        )
    expires_at = observed_at + evidence_lifetime
    if latest["outcome"] != "passed":
        return (
            "failed",
            [f"newest exact-release evidence outcome is {latest['outcome']}"],
            latest["evidence_id"],
            expires_at,
        )
    reasons = []
    for field in ("operation", "input_form", "output_form", "client_path"):
        if latest[field] != scenario[field]:
            reasons.append(f"evidence {field} differs from the advertised scenario")
    if latest["direct_runtime_only"] or not latest["public_endpoint_exercised"]:
        reasons.append("the real public customer endpoint was not exercised")
    if not latest["tenant_policy_exercised"]:
        reasons.append("the customer-equivalent tenant policy was not exercised")
    if not latest["terminal_outcome_validated"]:
        reasons.append("a terminal semantic outcome was not validated")
    if scenario["output_form"] != "none" and not latest["artifact_validated"]:
        reasons.append("the terminal output artifact was not validated")
    if latest["clean_cohorts"] < scenario["min_clean_cohorts"]:
        reasons.append("too few consecutive clean unchanged-release cohorts passed")
    for field in ("workload_states", "integrations"):
        missing = set(scenario[field]) - set(latest[field])
        if missing:
            reasons.append(f"evidence does not cover {field}: {sorted(missing)}")
    missing_surfaces = set(scenario["required_observability"]) - set(
        latest["observed_surfaces"]
    )
    if missing_surfaces:
        reasons.append(
            f"evidence does not cover observability surfaces: {sorted(missing_surfaces)}"
        )
    return (
        ("partial" if reasons else "qualified"),
        reasons,
        latest["evidence_id"],
        expires_at,
    )


def evaluate(
    raw_manifest: object,
    raw_evidence: list[object],
    *,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    manifest = validate_manifest(raw_manifest)
    evidence = [validate_evidence(item) for item in raw_evidence]
    now = (evaluated_at or datetime.now(UTC)).astimezone(UTC)
    if any(
        _timestamp(item["observed_at"], "evidence timestamp") > now for item in evidence
    ):
        raise CapabilityGateError("evidence timestamp cannot be in the future")
    if any(
        _timestamp(item["decided_at"], "decision timestamp") > now
        for item in manifest["reduced_scope_decisions"]
    ):
        raise CapabilityGateError(
            "reduced-scope decision timestamp cannot be in the future"
        )
    stale_before = now - timedelta(seconds=manifest["max_evidence_age_seconds"])
    exclusions = {item["capability_id"] for item in manifest["reduced_scope_decisions"]}
    requested = set(manifest["requested_capability_ids"])
    advertised = {item["id"] for item in manifest["capabilities"] if item["advertised"]}
    # A scope decision may remove a requested capability only after it is also
    # removed from the advertised customer contract. It cannot waive a claim
    # the platform continues to publish.
    required = advertised | (requested - exclusions)
    rows = []
    evidence_expirations: list[datetime] = []
    for capability in manifest["capabilities"]:
        scenarios = []
        for scenario in capability["scenarios"]:
            matching = sorted(
                (
                    item
                    for item in evidence
                    if item["app_id"] == manifest["app_id"]
                    and item["capability_id"] == capability["id"]
                    and item["scenario_id"] == scenario["id"]
                ),
                key=lambda item: _timestamp(item["observed_at"], "evidence timestamp"),
            )
            state, reasons, evidence_id, expires_at = _scenario_state(
                scenario,
                matching,
                manifest["release_identity"],
                stale_before,
                timedelta(seconds=manifest["max_evidence_age_seconds"]),
            )
            if expires_at is not None:
                evidence_expirations.append(expires_at)
            scenarios.append(
                {
                    "scenario_id": scenario["id"],
                    "state": state,
                    "evidence_id": evidence_id,
                    "reasons": reasons,
                }
            )
        scenario_states = {item["state"] for item in scenarios}
        if scenario_states == {"qualified"}:
            state = "qualified"
        elif "failed" in scenario_states:
            state = "failed"
        elif "stale" in scenario_states:
            state = "stale"
        elif scenario_states == {"untested"}:
            state = "untested"
        else:
            state = "partial"
        rows.append(
            {
                "capability_id": capability["id"],
                "advertised": capability["advertised"],
                "requested": capability["id"] in requested,
                "required": capability["id"] in required,
                "state": state,
                "scenarios": scenarios,
            }
        )
    required_rows = [row for row in rows if row["required"]]
    ready = bool(required_rows) and all(
        row["state"] == "qualified" for row in required_rows
    )
    # The projection must expire when its oldest contributing exact-release
    # receipt expires, not a full max-age interval after this evaluation. This
    # prevents repeatedly evaluating a 23-hour-old receipt from extending a
    # 24-hour customer-readiness claim to 47 hours.
    valid_until = min(
        evidence_expirations,
        default=now + timedelta(seconds=manifest["max_evidence_age_seconds"]),
    )
    return {
        "schema": VERDICT_SCHEMA,
        "app_id": manifest["app_id"],
        "evaluated_at": now.isoformat(),
        "valid_until": valid_until.isoformat(),
        "release_identity": manifest["release_identity"],
        "verdict": "customer-ready" if ready else "not-ready",
        "ready": ready,
        "required_capability_ids": sorted(required),
        "excluded_by_explicit_decision": sorted(exclusions),
        "capabilities": rows,
    }


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CapabilityGateError(f"cannot read valid JSON from {path}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args(argv)
    evidence_paths = list(args.evidence)
    if args.evidence_dir:
        evidence_paths.extend(sorted(args.evidence_dir.glob("*.json")))
    try:
        verdict = evaluate(
            _load(args.manifest), [_load(path) for path in evidence_paths]
        )
    except CapabilityGateError as error:
        print(f"capability gate input error: {error}", file=sys.stderr)
        return 3
    encoded = json.dumps(verdict, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 2 if args.require_ready and not verdict["ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
