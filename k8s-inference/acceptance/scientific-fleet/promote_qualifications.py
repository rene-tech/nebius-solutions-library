#!/usr/bin/env python3
"""Promote exact successful scientific fleet receipts into qualified profiles.

The live acceptance receipts are operator evidence and are not copied into the
repository.  This tool verifies their raw digests, immutable execution identity,
and per-stage scheduler admissions, then writes a secret-free model-owned
scheduler receipt and updates the canonical and model-owned profile projections
as one rollback-safe transaction.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

AGGREGATE_SCHEMA = "fs2-serve.nebius.ai/scientific-fleet-aggregate-receipt/v1"
MODEL_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-fleet-acceptance-receipt/v1"
ELIGIBILITY_SCHEMA = "fs2-serve.nebius.ai/scientific-scheduler-eligibility-receipt/v1"
PROFILE_SET_SCHEMA = "fs2-serve.nebius.ai/scientific-workload-profiles/v1"
PROFILE_SCHEMA = "fs2-serve.nebius.ai/scientific-workload-profile/v1"
EXECUTION_MAP_SCHEMA = "fs2-serve.nebius.ai/scientific-execution-map/v3"
PRIMARY_FRAGMENT_SCHEMA = "fs2.nebius.ai/primary-scientific-activation-fragment/v1"
SECONDARY_PROJECTION_SCHEMA = (
    "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1"
)
MAX_JSON_BYTES = 16 * 1024 * 1024
MODEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
TAGGED_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
PRIMARY_GLOB = "models/cancer-immunotherapy/**/activation/fragment.json"
SECONDARY_GLOB = "models/structure/batch-adapters/*/activation/workload-profile.json"
PROFILE_RELATIVE = Path("catalog/runtime/contracts/scientific-workload-profiles.json")
EXECUTION_MAP_RELATIVE = Path("catalog/runtime/contracts/scientific-execution-map.json")
PROFILE_SCHEMA_RELATIVE = Path(
    "catalog/runtime/schema/scientific-workload-profile.schema.json"
)
ELIGIBILITY_SCHEMA_RELATIVE = Path(
    "catalog/runtime/schema/scientific-scheduler-eligibility-receipt.schema.json"
)
PRIMARY_SCHEMA_RELATIVE = Path(
    "models/cancer-immunotherapy/primary-fleet-activation/fragment.schema.json"
)
PRIMARY_AWAITING_STATE = "semantic-qualified-active-awaiting-public-acceptance"
PRIMARY_QUALIFIED_STATE = "semantic-qualified-public-accepted"


class PromotionError(RuntimeError):
    """A stable, non-secret promotion failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ModelNotEligible(PromotionError):
    """Evidence for one model does not qualify that model for promotion."""


@dataclass(frozen=True, slots=True)
class ProfileOwner:
    model_id: str
    kind: Literal["primary-activation-fragment", "secondary-public-acceptance"]
    path: Path
    document: dict[str, Any]
    profile: dict[str, Any]
    acceptance_input_path: Path
    acceptance_input_relative: str
    eligibility_directory: Path
    primary: bool


@dataclass(frozen=True, slots=True)
class ModelDecision:
    model_id: str
    action: Literal["promote", "unchanged", "skip"]
    reason: str
    public_completion_receipt_sha256: str | None = None
    scheduler_eligibility_receipt_sha256: str | None = None
    qualified_at: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionResult:
    aggregate_sha256: str
    execution_map_sha256: str
    acceptance_execution_map_sha256: str
    decisions: tuple[ModelDecision, ...]
    written_paths: tuple[Path, ...]

    @property
    def promoted(self) -> int:
        return sum(item.action == "promote" for item in self.decisions)

    @property
    def unchanged(self) -> int:
        return sum(item.action == "unchanged" for item in self.decisions)

    @property
    def skipped(self) -> int:
        return sum(item.action == "skip" for item in self.decisions)


def _reject_constant(value: str) -> None:
    raise ValueError(f"forbidden JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _decode_json(raw: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise PromotionError(code) from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PromotionError(code)
    return cast(dict[str, Any], value)


def _read_json(
    path: Path,
    code: str,
    *,
    private: bool = False,
) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise PromotionError(code)
        if private and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PromotionError(code)
        if metadata.st_size > MAX_JSON_BYTES:
            raise PromotionError(code)
        raw = path.read_bytes()
    except PromotionError:
        raise
    except OSError as error:
        raise PromotionError(code) from error
    if len(raw) > MAX_JSON_BYTES:
        raise PromotionError(code)
    return _decode_json(raw, code), raw


def _object(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ModelNotEligible(code)
    return cast(dict[str, Any], value)


def _items(value: object, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise ModelNotEligible(code)
    return value


def _text(value: object, code: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ModelNotEligible(code)
    return value


def _raw_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ModelNotEligible(code)
    return value


def _tagged_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or TAGGED_SHA256_RE.fullmatch(value) is None:
        raise ModelNotEligible(code)
    return value


def _timestamp(value: object, code: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or RFC3339_RE.fullmatch(value) is None:
        raise ModelNotEligible(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ModelNotEligible(code) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelNotEligible(code)
    return value, parsed.astimezone(UTC)


def _canonical_bytes(value: object, *, newline: bool = False) -> bytes:
    suffix = "\n" if newline else ""
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + suffix
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PromotionError("document_not_canonicalizable") from error


def _pretty_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PromotionError("document_not_canonicalizable") from error


def _helm_to_json_bytes(value: object) -> bytes:
    """Match Go encoding/json bytes used by Helm/Sprig ``toJson``."""

    rendered = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    for character, escape in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        rendered = rendered.replace(character, escape)
    return rendered.encode("utf-8")


def _relative(root: Path, path: Path, code: str) -> str:
    try:
        return path.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise PromotionError(code) from error


def _discover_owners(root: Path) -> dict[str, ProfileOwner]:
    owners: dict[str, ProfileOwner] = {}
    for primary, pattern in ((True, PRIMARY_GLOB), (False, SECONDARY_GLOB)):
        for path in sorted(root.glob(pattern)):
            document, raw = _read_json(path, "profile_owner_invalid")
            if raw != _pretty_bytes(document):
                raise PromotionError("profile_owner_not_canonical")
            if primary:
                if document.get("schema") != PRIMARY_FRAGMENT_SCHEMA:
                    continue
                projection = _object(
                    document.get("profile_projection"), "profile_owner_invalid"
                )
                profile = _object(projection.get("profile"), "profile_owner_invalid")
                model_id = document.get("model_id")
                kind: Literal[
                    "primary-activation-fragment", "secondary-public-acceptance"
                ] = "primary-activation-fragment"
                acceptance_input = path
            else:
                if document.get("schema") != SECONDARY_PROJECTION_SCHEMA:
                    continue
                if (
                    set(document) != {"schema", "merge_target", "profile"}
                    or document.get("merge_target") != PROFILE_RELATIVE.as_posix()
                ):
                    raise PromotionError("profile_owner_invalid")
                profile = _object(document.get("profile"), "profile_owner_invalid")
                model_id = profile.get("model_id")
                kind = "secondary-public-acceptance"
                acceptance_input = path.with_name("public-acceptance.json")
            if not isinstance(model_id, str) or MODEL_RE.fullmatch(model_id) is None:
                raise PromotionError("profile_owner_model_invalid")
            if model_id in owners:
                raise PromotionError("profile_owner_duplicate")
            acceptance_relative = _relative(
                root, acceptance_input, "acceptance_input_path_invalid"
            )
            owners[model_id] = ProfileOwner(
                model_id=model_id,
                kind=kind,
                path=path,
                document=document,
                profile=profile,
                acceptance_input_path=acceptance_input,
                acceptance_input_relative=acceptance_relative,
                eligibility_directory=path.parent / "qualification",
                primary=primary,
            )
    return owners


def _validate_profile(
    profile: dict[str, Any],
    *,
    validator: Draft202012Validator,
    execution: dict[str, Any],
    execution_map_sha256: str,
) -> None:
    errors = sorted(
        validator.iter_errors(profile),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise ModelNotEligible("profile_schema_invalid")
    model_id = profile.get("model_id")
    identity = _object(profile.get("execution_identity"), "profile_identity_invalid")
    identity_payload = dict(identity)
    recorded_identity = identity_payload.pop("execution_identity_sha256", None)
    if (
        recorded_identity
        != hashlib.sha256(_canonical_bytes(identity_payload)).hexdigest()
    ):
        raise ModelNotEligible("profile_identity_digest_stale")
    workload = _object(profile.get("workload"), "profile_workload_invalid")
    if (
        identity.get("workload_recipe_sha256")
        != hashlib.sha256(_canonical_bytes(workload)).hexdigest()
    ):
        raise ModelNotEligible("profile_workload_digest_stale")
    if (
        execution.get("model_id") != model_id
        or execution.get("execution_identity_sha256") != recorded_identity
    ):
        raise ModelNotEligible("execution_map_identity_mismatch")
    qualification = _object(profile.get("qualification"), "qualification_missing")
    if qualification.get("execution_map_sha256") != execution_map_sha256:
        raise ModelNotEligible("execution_map_digest_mismatch")
    profile_stages = _items(workload.get("stages"), "profile_stages_invalid")
    execution_stages = _items(execution.get("stages"), "execution_stages_invalid")
    if [item.get("id") for item in profile_stages if isinstance(item, dict)] != [
        item.get("stage_id") for item in execution_stages if isinstance(item, dict)
    ] or len(profile_stages) != len(execution_stages):
        raise ModelNotEligible("execution_map_stage_mismatch")


def _load_repository(
    root: Path,
) -> tuple[
    Path,
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, ProfileOwner],
    str,
]:
    profile_path = root / PROFILE_RELATIVE
    execution_path = root / EXECUTION_MAP_RELATIVE
    schema_path = root / PROFILE_SCHEMA_RELATIVE
    profiles_document, profiles_raw = _read_json(
        profile_path, "profile_catalog_invalid"
    )
    execution_document, execution_raw = _read_json(
        execution_path, "execution_map_invalid"
    )
    schema, _ = _read_json(schema_path, "profile_schema_unavailable")
    primary_schema, _ = _read_json(
        root / PRIMARY_SCHEMA_RELATIVE, "primary_fragment_schema_unavailable"
    )
    if profiles_raw != _pretty_bytes(profiles_document):
        raise PromotionError("profile_catalog_not_canonical")
    if execution_raw != _pretty_bytes(execution_document):
        raise PromotionError("execution_map_not_canonical")
    if profiles_document.get("schema") != PROFILE_SET_SCHEMA:
        raise PromotionError("profile_catalog_schema_invalid")
    if execution_document.get("schema") != EXECUTION_MAP_SCHEMA:
        raise PromotionError("execution_map_schema_invalid")
    raw_profiles = profiles_document.get("profiles")
    raw_executions = execution_document.get("models")
    if not isinstance(raw_profiles, list) or not isinstance(raw_executions, list):
        raise PromotionError("catalog_shape_invalid")
    profiles: dict[str, dict[str, Any]] = {
        cast(str, item.get("model_id")): item
        for item in raw_profiles
        if isinstance(item, dict) and isinstance(item.get("model_id"), str)
    }
    executions: dict[str, dict[str, Any]] = {
        cast(str, item.get("model_id")): item
        for item in raw_executions
        if isinstance(item, dict) and isinstance(item.get("model_id"), str)
    }
    if (
        len(profiles) != len(raw_profiles)
        or len(executions) != len(raw_executions)
        or set(profiles) != set(executions)
    ):
        raise PromotionError("catalog_model_set_invalid")
    owners = _discover_owners(root)
    if set(owners) != set(profiles):
        raise PromotionError("profile_owner_set_invalid")
    execution_map_sha256 = hashlib.sha256(
        _helm_to_json_bytes(execution_document)
    ).hexdigest()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    primary_validator = Draft202012Validator(
        primary_schema, format_checker=FormatChecker()
    )
    for model_id, profile in profiles.items():
        owner = owners[model_id]
        if owner.profile != profile:
            raise PromotionError("profile_owner_projection_drift")
        if owner.primary and list(primary_validator.iter_errors(owner.document)):
            raise PromotionError("primary_fragment_schema_invalid")
        try:
            _validate_profile(
                profile,
                validator=validator,
                execution=executions[model_id],
                execution_map_sha256=execution_map_sha256,
            )
        except ModelNotEligible as error:
            raise PromotionError(error.code) from error
    return (
        profile_path,
        profiles_document,
        profiles,
        executions,
        owners,
        execution_map_sha256,
    )


def _validate_aggregate(
    aggregate_path: Path,
    *,
    profiles: dict[str, dict[str, Any]],
    owners: dict[str, ProfileOwner],
) -> tuple[dict[str, Any], bytes, dict[str, dict[str, Any]]]:
    aggregate, raw = _read_json(aggregate_path, "aggregate_invalid", private=True)
    if raw != _canonical_bytes(aggregate, newline=True):
        raise PromotionError("aggregate_not_canonical")
    if aggregate.get("schema") != AGGREGATE_SCHEMA:
        raise PromotionError("aggregate_schema_invalid")
    if set(aggregate) != {"schema", "run_id", "endpoint", "summary", "models"}:
        raise PromotionError("aggregate_shape_invalid")
    try:
        _text(aggregate.get("run_id"), "aggregate_run_id_invalid", maximum=128)
    except ModelNotEligible as error:
        raise PromotionError(error.code) from error
    endpoint = aggregate.get("endpoint")
    if (
        not isinstance(endpoint, dict)
        or set(endpoint) != {"host", "tls"}
        or not isinstance(endpoint.get("host"), str)
        or not endpoint["host"]
        or not isinstance(endpoint.get("tls"), bool)
    ):
        raise PromotionError("aggregate_endpoint_invalid")
    values = aggregate.get("models")
    summary = aggregate.get("summary")
    if not isinstance(values, list) or not isinstance(summary, dict):
        raise PromotionError("aggregate_shape_invalid")
    rows: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            raise PromotionError("aggregate_model_invalid")
        model_id = value.get("model_id")
        if not isinstance(model_id, str) or MODEL_RE.fullmatch(model_id) is None:
            raise PromotionError("aggregate_model_invalid")
        if model_id in rows:
            raise PromotionError("aggregate_model_duplicate")
        rows[model_id] = value
        ordered.append(model_id)
    if ordered != sorted(ordered) or set(rows) != set(profiles):
        raise PromotionError("aggregate_model_set_invalid")
    succeeded = sum(row.get("status") == "succeeded" for row in rows.values())
    failed = sum(row.get("status") == "failed" for row in rows.values())
    if succeeded + failed != len(rows):
        raise PromotionError("aggregate_status_invalid")
    expected_summary = {
        "discovered": len(rows),
        "primary": sum(owner.primary for owner in owners.values()),
        "secondary": sum(not owner.primary for owner in owners.values()),
        "succeeded": succeeded,
        "failed": failed,
    }
    if set(summary) != {*expected_summary, "max_parallel"}:
        raise PromotionError("aggregate_summary_invalid")
    if any(summary.get(key) != value for key, value in expected_summary.items()):
        raise PromotionError("aggregate_summary_invalid")
    maximum = summary.get("max_parallel")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        raise PromotionError("aggregate_summary_invalid")
    for model_id, row in rows.items():
        source = row.get("input")
        owner = owners[model_id]
        expected_keys = (
            {
                "model_id",
                "input",
                "status",
                "receipt",
                "operation_identity",
                "terminal_state",
                "execution_identity",
                "api_measurements",
            }
            if row.get("status") == "succeeded"
            else {"model_id", "input", "status", "error_code", "api_measurements"}
        )
        if set(row) != expected_keys:
            raise PromotionError("aggregate_model_invalid")
        if not isinstance(source, dict) or source != {
            "kind": owner.kind,
            "path": owner.acceptance_input_relative,
            "sha256": source.get("sha256"),
        }:
            raise PromotionError("aggregate_input_identity_invalid")
        digest = source.get("sha256")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise PromotionError("aggregate_input_identity_invalid")
        if row.get("status") == "failed" and row.get("api_measurements") is not None:
            raise PromotionError("aggregate_failure_row_invalid")
    return aggregate, raw, rows


def _active_owner_document(
    owner: ProfileOwner, eligibility: dict[str, Any]
) -> dict[str, Any]:
    """Reconstruct a model owner's exact pre-promotion active projection."""

    document = copy.deepcopy(owner.document)
    if owner.primary:
        projection = _object(
            document.get("profile_projection"), "qualified_owner_invalid"
        )
        profile = _object(projection.get("profile"), "qualified_owner_invalid")
    else:
        profile = _object(document.get("profile"), "qualified_owner_invalid")
    qualification = _object(profile.get("qualification"), "qualified_owner_invalid")
    profile["state"] = "active"
    _object(profile.get("semantic_validation"), "qualified_owner_invalid")["state"] = (
        "active"
    )
    qualification["public_completion_receipt_sha256"] = None
    qualification["scheduler_eligibility_receipt_sha256"] = None
    qualification["qualified_at"] = eligibility.get("prior_profile_qualified_at")
    if owner.primary:
        _object(
            _object(document.get("accepted_evidence"), "qualified_owner_invalid").get(
                "h100"
            ),
            "qualified_owner_invalid",
        )["state"] = PRIMARY_AWAITING_STATE
        _object(document.get("activation_gate"), "qualified_owner_invalid")
        document["activation_gate"]["public_platform_run_required"] = True
    return document


def _active_input_bytes(owner: ProfileOwner, eligibility: dict[str, Any]) -> bytes:
    """Reconstruct the exact pre-promotion primary input for idempotence."""

    return _pretty_bytes(_active_owner_document(owner, eligibility))


def _eligibility_path(owner: ProfileOwner, digest: str) -> Path:
    return owner.eligibility_directory / f"scheduler-eligibility-{digest}.json"


def _existing_eligibility(
    owner: ProfileOwner, profile: dict[str, Any]
) -> tuple[dict[str, Any], bytes]:
    qualification = _object(profile.get("qualification"), "qualified_profile_invalid")
    digest = _raw_sha256(
        qualification.get("scheduler_eligibility_receipt_sha256"),
        "qualified_scheduler_receipt_invalid",
    )
    try:
        document, raw = _read_json(
            _eligibility_path(owner, digest), "qualified_scheduler_receipt_invalid"
        )
    except PromotionError as error:
        raise ModelNotEligible(error.code) from error
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ModelNotEligible("qualified_scheduler_receipt_digest_mismatch")
    return document, raw


def _validate_acceptance_input(
    owner: ProfileOwner,
    profile: dict[str, Any],
    aggregate_input_sha256: str,
    *,
    acceptance_owner: ProfileOwner,
    acceptance_profile: dict[str, Any],
    execution: dict[str, Any],
    acceptance_execution: dict[str, Any],
) -> None:
    try:
        _, raw = _read_json(owner.acceptance_input_path, "acceptance_input_invalid")
    except PromotionError as error:
        raise ModelNotEligible(error.code) from error
    current_digest = hashlib.sha256(raw).hexdigest()
    if acceptance_owner is owner:
        if current_digest == aggregate_input_sha256:
            return
        if not owner.primary or profile.get("state") != "qualified":
            raise ModelNotEligible("acceptance_input_digest_mismatch")
        eligibility, _ = _existing_eligibility(owner, profile)
        if (
            hashlib.sha256(_active_input_bytes(owner, eligibility)).hexdigest()
            != aggregate_input_sha256
        ):
            raise ModelNotEligible("acceptance_input_digest_mismatch")
        return

    if (
        owner.kind != acceptance_owner.kind
        or owner.acceptance_input_relative != acceptance_owner.acceptance_input_relative
    ):
        raise ModelNotEligible("acceptance_input_identity_drift")
    try:
        _, acceptance_raw = _read_json(
            acceptance_owner.acceptance_input_path, "acceptance_input_invalid"
        )
    except PromotionError as error:
        raise ModelNotEligible(error.code) from error
    if hashlib.sha256(acceptance_raw).hexdigest() != aggregate_input_sha256:
        raise ModelNotEligible("acceptance_input_digest_mismatch")
    if execution != acceptance_execution:
        raise ModelNotEligible("acceptance_model_execution_map_drift")

    accepted_state = acceptance_profile.get("state")
    current_state = profile.get("state")
    current_owner_document = copy.deepcopy(owner.document)
    if accepted_state == "active" and current_state == "qualified":
        eligibility, _ = _existing_eligibility(owner, profile)
        current_owner_document = _active_owner_document(owner, eligibility)
    elif current_state != accepted_state:
        raise ModelNotEligible("acceptance_model_profile_drift")

    current_owner_profile = (
        _object(
            _object(
                current_owner_document.get("profile_projection"),
                "acceptance_model_profile_drift",
            ).get("profile"),
            "acceptance_model_profile_drift",
        )
        if owner.primary
        else _object(
            current_owner_document.get("profile"),
            "acceptance_model_profile_drift",
        )
    )
    accepted_qualification = _object(
        acceptance_profile.get("qualification"),
        "acceptance_model_profile_drift",
    )
    current_qualification = _object(
        current_owner_profile.get("qualification"),
        "acceptance_model_profile_drift",
    )
    current_qualification["execution_map_sha256"] = accepted_qualification.get(
        "execution_map_sha256"
    )
    if current_owner_document != acceptance_owner.document:
        raise ModelNotEligible("acceptance_model_profile_drift")

    # Secondary acceptance inputs are separate from their owner projection.
    # Any bytes changed there are a real acceptance-contract change, not the
    # harmless fleet-wide execution-map digest fan-out handled above.
    if (
        owner.acceptance_input_path != owner.path
        and current_digest != aggregate_input_sha256
    ):
        raise ModelNotEligible("acceptance_input_contract_drift")


def _gpu_attribution(receipt: dict[str, Any]) -> dict[str, Any]:
    """Reproduce the aggregate runner's exact no-inference projection."""

    for field in (
        "gpu_occupied_idle",
        "gpu_accounting",
        "lifecycle_accounting",
        "resource_accounting",
    ):
        value = receipt.get(field)
        if isinstance(value, dict) and any(
            "occupied" in key or "idle" in key for key in value
        ):
            return {"available": True, "source_field": field, "value": value}
    operation_accounting = receipt.get("operation_accounting")
    if isinstance(operation_accounting, dict) and any(
        field in operation_accounting
        for field in (
            "scheduler_occupied_gpu_seconds",
            "device_allocated_gpu_seconds",
            "active_gpu_seconds",
            "occupied_idle_gpu_seconds",
        )
    ):
        return {
            "available": True,
            "source_field": "operation_accounting",
            "value": operation_accounting,
        }
    return {"available": False, "source_field": None, "value": None}


def _validate_receipt_projections(row: dict[str, Any], receipt: dict[str, Any]) -> None:
    if row.get("operation_identity") != receipt.get("operation_identity"):
        raise ModelNotEligible("aggregate_receipt_projection_mismatch")
    if row.get("terminal_state") != receipt.get("terminal_state"):
        raise ModelNotEligible("aggregate_receipt_projection_mismatch")
    if row.get("execution_identity") != receipt.get("execution_identity"):
        raise ModelNotEligible("aggregate_receipt_projection_mismatch")
    cold_start = _object(receipt.get("cold_start"), "model_receipt_incomplete")
    expected_measurements = {
        "cold_start": cold_start,
        "runtime": {
            "runtime_identity": cold_start.get("runtime"),
            "timestamps": receipt.get("timestamps"),
            "attempts": receipt.get("attempts"),
        },
        "queue": receipt.get("queue"),
        "gpu_occupied_idle": _gpu_attribution(receipt),
    }
    if row.get("api_measurements") != expected_measurements:
        raise ModelNotEligible("aggregate_receipt_projection_mismatch")


def _validate_execution_identity(
    *,
    model_id: str,
    profile: dict[str, Any],
    execution: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    identity = _object(receipt.get("execution_identity"), "receipt_identity_invalid")
    expected = _object(profile.get("execution_identity"), "profile_identity_invalid")
    expected_result = {
        "model_id": model_id,
        "variant_id": execution.get("variant_id"),
        "model_revision": expected.get("model_revision"),
        "runtime_image_digest": expected.get("runtime_image_digest"),
        "runtime_recipe_sha256": expected.get("runtime_recipe_sha256"),
        "workload_recipe_sha256": expected.get("workload_recipe_sha256"),
        "model_artifact_manifest_digest": expected.get("artifact_manifest_digest"),
        "execution_identity_sha256": expected.get("execution_identity_sha256"),
    }
    if identity != expected_result:
        raise ModelNotEligible("receipt_execution_identity_mismatch")
    model = _object(receipt.get("model"), "receipt_model_identity_invalid")
    if model != {
        "model_id": model_id,
        "variant_id": execution.get("variant_id"),
    }:
        raise ModelNotEligible("receipt_model_identity_invalid")


def _validate_terminal(receipt: dict[str, Any]) -> tuple[str, datetime]:
    terminal = _object(receipt.get("terminal_state"), "receipt_terminal_invalid")
    if terminal != {
        "operation": "succeeded",
        "batch": "succeeded",
        "result": "succeeded",
        "semantic_validation": "passed",
    }:
        raise ModelNotEligible("receipt_terminal_invalid")
    timestamps = _object(receipt.get("timestamps"), "receipt_timestamps_invalid")
    accepted_text, accepted = _timestamp(
        timestamps.get("accepted_at"), "receipt_timestamps_invalid"
    )
    del accepted_text
    completed_text, completed = _timestamp(
        timestamps.get("result_completed_at"), "receipt_timestamps_invalid"
    )
    if completed < accepted:
        raise ModelNotEligible("receipt_timestamps_invalid")
    artifacts = _object(receipt.get("artifact_digests"), "receipt_artifacts_invalid")
    _raw_sha256(
        artifacts.get("semantic_validation_receipt_sha256"),
        "receipt_semantic_digest_invalid",
    )
    for field in ("input_manifest", "output_manifest"):
        pointer = _object(artifacts.get(field), "receipt_artifacts_invalid")
        _raw_sha256(pointer.get("sha256"), "receipt_artifacts_invalid")
    return completed_text, completed


def _successful_admissions(
    *,
    model_id: str,
    profile: dict[str, Any],
    receipt: dict[str, Any],
    completed: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    queue = _object(receipt.get("queue"), "receipt_queue_invalid")
    snapshot_digest = _tagged_sha256(
        queue.get("scheduling_snapshot_digest"), "scheduler_snapshot_digest_invalid"
    )
    _text(queue.get("policy_revision"), "scheduler_policy_invalid", maximum=200)
    _, captured = _timestamp(queue.get("captured_at"), "scheduler_timestamp_invalid")
    if captured > completed:
        raise ModelNotEligible("scheduler_timestamp_invalid")
    interface = _object(profile.get("interface"), "profile_interface_invalid")
    service_classes = _items(
        interface.get("service_classes"), "profile_service_classes_invalid"
    )
    if queue.get("service_class") not in service_classes:
        raise ModelNotEligible("scheduler_service_class_mismatch")
    if queue.get("model_lane") != model_id:
        raise ModelNotEligible("scheduler_model_lane_mismatch")
    _text(queue.get("tenant_queue"), "scheduler_tenant_queue_invalid", maximum=128)

    workload = _object(profile.get("workload"), "profile_workload_invalid")
    profile_stages = {
        item.get("id"): item
        for item in _items(workload.get("stages"), "profile_stages_invalid")
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    decisions = _items(queue.get("stage_decisions"), "scheduler_decisions_invalid")
    by_stage = {
        item.get("stage_id"): item
        for item in decisions
        if isinstance(item, dict) and isinstance(item.get("stage_id"), str)
    }
    selected_stage_ids = tuple(by_stage)
    selected_stage_set = set(selected_stage_ids)
    if (
        not selected_stage_ids
        or len(by_stage) != len(decisions)
        or not selected_stage_set.issubset(profile_stages)
    ):
        raise ModelNotEligible("scheduler_stage_set_mismatch")
    canonical_selected_order = tuple(
        stage_id for stage_id in profile_stages if stage_id in selected_stage_set
    )
    if selected_stage_ids != canonical_selected_order:
        raise ModelNotEligible("scheduler_stage_order_mismatch")
    for stage_id in selected_stage_ids:
        needs = profile_stages[stage_id].get("needs")
        if not isinstance(needs, list) or not all(
            isinstance(dependency, str) for dependency in needs
        ):
            raise ModelNotEligible("profile_stage_dependencies_invalid")
        if not set(needs).issubset(selected_stage_set):
            raise ModelNotEligible("scheduler_stage_dependency_mismatch")
    resources = _object(profile.get("resources"), "profile_resources_invalid")
    compatible_pools = resources.get("compatible_pool_ids")
    gpu_count = resources.get("gpu_count")
    if not isinstance(compatible_pools, list) or not isinstance(gpu_count, int):
        raise ModelNotEligible("profile_resources_invalid")
    for stage_id in selected_stage_ids:
        expected = profile_stages[stage_id]
        decision = cast(dict[str, Any], by_stage[stage_id])
        if decision.get("resource_class") != expected.get(
            "resource_class"
        ) or decision.get("checkpoint_mode") != expected.get("checkpoint_mode"):
            raise ModelNotEligible("scheduler_stage_contract_mismatch")
        pools = decision.get("resolved_pool_preference")
        if not isinstance(pools, list) or len(pools) != len(set(pools)):
            raise ModelNotEligible("scheduler_pool_preference_invalid")
        if expected.get("resource_class") == "gpu":
            if (
                pools != compatible_pools
                or decision.get("accelerator_resource_name") != "nvidia.com/gpu"
                or decision.get("accelerator_count") != gpu_count
            ):
                raise ModelNotEligible("scheduler_gpu_decision_mismatch")
        elif (
            decision.get("accelerator_resource_name") is not None
            or decision.get("accelerator_count") != 0
        ):
            raise ModelNotEligible("scheduler_cpu_decision_mismatch")

    observed_values = _items(
        queue.get("observed_stages"), "scheduler_observations_invalid"
    )
    observed = {
        item.get("stage_id"): item
        for item in observed_values
        if isinstance(item, dict) and isinstance(item.get("stage_id"), str)
    }
    if len(observed) != len(observed_values) or tuple(observed) != selected_stage_ids:
        raise ModelNotEligible("scheduler_observed_stage_set_mismatch")
    if any(item.get("status") != "succeeded" for item in observed.values()):
        raise ModelNotEligible("scheduler_observed_stage_failed")

    attempts = _items(receipt.get("attempts"), "receipt_attempts_invalid")
    result_by_id: dict[str, dict[str, Any]] = {}
    for item in attempts:
        attempt = _object(item, "receipt_attempt_invalid")
        attempt_id = _text(
            attempt.get("attempt_id"), "receipt_attempt_invalid", maximum=128
        )
        if attempt_id in result_by_id:
            raise ModelNotEligible("receipt_attempt_duplicate")
        result_by_id[attempt_id] = attempt
    observed_by_id: dict[str, dict[str, Any]] = {}
    for stage_id, stage in observed.items():
        for item in _items(stage.get("attempts"), "scheduler_observations_invalid"):
            attempt = _object(item, "scheduler_observations_invalid")
            attempt_id = _text(
                attempt.get("attempt_id"),
                "scheduler_observations_invalid",
                maximum=128,
            )
            if attempt_id in observed_by_id:
                raise ModelNotEligible("scheduler_observation_identity_invalid")
            observed_by_id[attempt_id] = {**attempt, "stage_id": stage_id}
    if set(result_by_id) != set(observed_by_id):
        raise ModelNotEligible("scheduler_attempt_projection_mismatch")

    promoted: list[dict[str, Any]] = []
    succeeded_stages: set[str] = set()
    for attempt_id, attempt in result_by_id.items():
        observed_attempt = observed_by_id[attempt_id]
        status = attempt.get("status")
        expected_outcome = "succeeded" if status == "succeeded" else status
        if (
            observed_attempt.get("stage_id") != attempt.get("stage_id")
            or observed_attempt.get("shard_id") != attempt.get("shard_id")
            or observed_attempt.get("attempt_number") != attempt.get("attempt_number")
            or observed_attempt.get("outcome") != expected_outcome
            or observed_attempt.get("scheduling_admission")
            != attempt.get("scheduling_admission")
        ):
            raise ModelNotEligible("scheduler_attempt_projection_mismatch")
        if status != "succeeded":
            continue
        stage_id = attempt.get("stage_id")
        if stage_id not in profile_stages:
            raise ModelNotEligible("scheduler_attempt_stage_invalid")
        admission = _object(
            attempt.get("scheduling_admission"), "scheduler_admission_missing"
        )
        decision = cast(dict[str, Any], by_stage[stage_id])
        _, admitted = _timestamp(
            admission.get("admitted_at"), "scheduler_admission_invalid"
        )
        if admitted > completed:
            raise ModelNotEligible("scheduler_admission_invalid")
        expected_stage = profile_stages[stage_id]
        if expected_stage.get("resource_class") == "gpu":
            if (
                admission.get("resolved_pool_id")
                not in decision["resolved_pool_preference"]
                or admission.get("admitted_resource_flavor") is None
                or admission.get("accelerator_resource_name")
                != decision.get("accelerator_resource_name")
                or admission.get("accelerator_count")
                != decision.get("accelerator_count")
            ):
                raise ModelNotEligible("scheduler_gpu_admission_mismatch")
        elif any(
            (
                admission.get("resolved_pool_id") is not None,
                admission.get("admitted_resource_flavor") is not None,
                admission.get("accelerator_resource_name") is not None,
                admission.get("accelerator_count") != 0,
            )
        ):
            raise ModelNotEligible("scheduler_cpu_admission_mismatch")
        attempt_number = attempt.get("attempt_number")
        if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
            raise ModelNotEligible("scheduler_admission_invalid")
        promoted.append(
            {
                "stage_id": stage_id,
                "shard_id": attempt.get("shard_id"),
                "attempt_number": attempt_number,
                "resolved_pool_id": admission.get("resolved_pool_id"),
                "admitted_resource_flavor": admission.get("admitted_resource_flavor"),
                "accelerator_resource_name": admission.get("accelerator_resource_name"),
                "accelerator_count": admission.get("accelerator_count"),
                "admitted_at": admission.get("admitted_at"),
            }
        )
        succeeded_stages.add(cast(str, stage_id))
    if succeeded_stages != selected_stage_set:
        raise ModelNotEligible("scheduler_stage_success_incomplete")
    promoted.sort(
        key=lambda item: (
            item["stage_id"],
            str(item["shard_id"] or ""),
            item["attempt_number"],
            item["admitted_at"],
        )
    )
    # The private receipt is validated above with its exact provider pool
    # decision intact.  CPU pool IDs are infrastructure identities rather than
    # portable scheduler eligibility, so omit them from the checked-in public
    # projection.  GPU preferences are logical, model-qualified pool IDs and
    # remain necessary to prove accelerator eligibility.
    projected_decisions = copy.deepcopy(decisions)
    for decision in projected_decisions:
        if decision["resource_class"] == "cpu":
            decision["resolved_pool_preference"] = []
    queue_projection = {
        "digest": snapshot_digest,
        "policy_revision": queue.get("policy_revision"),
        "captured_at": queue.get("captured_at"),
        "service_class": queue.get("service_class"),
        "tenant_queue": queue.get("tenant_queue"),
        "model_lane": queue.get("model_lane"),
        "stage_decisions": projected_decisions,
    }
    return queue_projection, promoted


def _scheduler_receipt(
    *,
    model_id: str,
    profile: dict[str, Any],
    execution: dict[str, Any],
    row: dict[str, Any],
    public_digest: str,
    aggregate_digest: str,
    qualified_at: str,
    scheduling: dict[str, Any],
    admissions: list[dict[str, Any]],
    prior_profile_qualified_at: str,
    acceptance_execution_map_sha256: str,
    model_execution_map_entry_sha256: str,
) -> dict[str, Any]:
    qualification = _object(profile.get("qualification"), "qualification_missing")
    identity = _object(profile.get("execution_identity"), "profile_identity_invalid")
    source = _object(row.get("input"), "aggregate_input_identity_invalid")
    return {
        "schema": ELIGIBILITY_SCHEMA,
        "model_id": model_id,
        "variant_id": execution.get("variant_id"),
        "execution_identity_sha256": identity.get("execution_identity_sha256"),
        "execution_map_sha256": qualification.get("execution_map_sha256"),
        "acceptance_execution_map_sha256": acceptance_execution_map_sha256,
        "model_execution_map_entry_sha256": model_execution_map_entry_sha256,
        "public_completion_receipt_sha256": public_digest,
        "fleet_aggregate_sha256": aggregate_digest,
        "acceptance_input": {
            "kind": source.get("kind"),
            "path": source.get("path"),
            "sha256": source.get("sha256"),
        },
        "scheduling_snapshot": scheduling,
        "successful_admissions": admissions,
        "prior_profile_qualified_at": prior_profile_qualified_at,
        "qualified_at": qualified_at,
    }


def _promote_profile(
    profile: dict[str, Any],
    *,
    public_digest: str,
    scheduler_digest: str,
    qualified_at: str,
) -> None:
    profile["state"] = "qualified"
    _object(profile.get("semantic_validation"), "profile_semantic_invalid")["state"] = (
        "qualified"
    )
    qualification = _object(profile.get("qualification"), "qualification_missing")
    qualification["public_completion_receipt_sha256"] = public_digest
    qualification["scheduler_eligibility_receipt_sha256"] = scheduler_digest
    qualification["qualified_at"] = qualified_at


def _promote_owner(
    owner: ProfileOwner,
    profile: dict[str, Any],
) -> dict[str, Any]:
    document = copy.deepcopy(owner.document)
    if owner.primary:
        projection = _object(
            document.get("profile_projection"), "profile_owner_invalid"
        )
        projection["profile"] = copy.deepcopy(profile)
        _object(
            _object(document.get("accepted_evidence"), "profile_owner_invalid").get(
                "h100"
            ),
            "profile_owner_invalid",
        )["state"] = PRIMARY_QUALIFIED_STATE
        _object(document.get("activation_gate"), "profile_owner_invalid")[
            "public_platform_run_required"
        ] = False
    else:
        document["profile"] = copy.deepcopy(profile)
    return document


def _evaluate_model(
    *,
    model_id: str,
    row: dict[str, Any],
    aggregate: dict[str, Any],
    aggregate_digest: str,
    aggregate_directory: Path,
    profile: dict[str, Any],
    execution: dict[str, Any],
    owner: ProfileOwner,
    acceptance_profile: dict[str, Any],
    acceptance_execution: dict[str, Any],
    acceptance_owner: ProfileOwner,
    acceptance_execution_map_sha256: str,
    profile_validator: Draft202012Validator,
    eligibility_validator: Draft202012Validator,
) -> tuple[ModelDecision, dict[str, Any] | None, bytes | None, Path | None]:
    if row.get("status") != "succeeded":
        return ModelDecision(model_id, "skip", "acceptance_failed"), None, None, None
    source = _object(row.get("input"), "aggregate_input_identity_invalid")
    _validate_acceptance_input(
        owner,
        profile,
        _raw_sha256(source.get("sha256"), "aggregate_input_identity_invalid"),
        acceptance_owner=acceptance_owner,
        acceptance_profile=acceptance_profile,
        execution=execution,
        acceptance_execution=acceptance_execution,
    )
    receipt_ref = _object(row.get("receipt"), "aggregate_receipt_ref_invalid")
    expected_path = f"{model_id}.json"
    if receipt_ref.get("path") != expected_path:
        raise ModelNotEligible("aggregate_receipt_path_invalid")
    public_digest = _raw_sha256(
        receipt_ref.get("sha256"), "aggregate_receipt_digest_invalid"
    )
    receipt_path = aggregate_directory / expected_path
    try:
        receipt, receipt_raw = _read_json(
            receipt_path, "model_receipt_invalid", private=True
        )
    except PromotionError as error:
        raise ModelNotEligible(error.code) from error
    if (
        len(receipt_raw) != receipt_ref.get("size_bytes")
        or hashlib.sha256(receipt_raw).hexdigest() != public_digest
    ):
        raise ModelNotEligible("model_receipt_digest_mismatch")
    if receipt.get("schema") != MODEL_RECEIPT_SCHEMA:
        raise ModelNotEligible("model_receipt_schema_invalid")
    if receipt.get("endpoint") != aggregate.get("endpoint"):
        raise ModelNotEligible("model_receipt_endpoint_mismatch")
    _validate_receipt_projections(row, receipt)
    _validate_execution_identity(
        model_id=model_id,
        profile=profile,
        execution=execution,
        receipt=receipt,
    )
    qualified_at, completed = _validate_terminal(receipt)
    scheduling, admissions = _successful_admissions(
        model_id=model_id,
        profile=profile,
        receipt=receipt,
        completed=completed,
    )
    state = profile.get("state")
    qualification = _object(profile.get("qualification"), "qualification_missing")
    existing_eligibility: dict[str, Any] | None = None
    existing_eligibility_raw: bytes | None = None
    prior_profile_qualified_at = _timestamp(
        qualification.get("qualified_at"), "prior_profile_qualified_at_invalid"
    )[0]
    if state == "qualified":
        existing_eligibility, existing_eligibility_raw = _existing_eligibility(
            owner, profile
        )
        prior_profile_qualified_at = _timestamp(
            existing_eligibility.get("prior_profile_qualified_at"),
            "qualified_scheduler_receipt_invalid",
        )[0]
    eligibility = _scheduler_receipt(
        model_id=model_id,
        profile=profile,
        execution=execution,
        row=row,
        public_digest=public_digest,
        aggregate_digest=aggregate_digest,
        qualified_at=qualified_at,
        scheduling=scheduling,
        admissions=admissions,
        prior_profile_qualified_at=prior_profile_qualified_at,
        acceptance_execution_map_sha256=acceptance_execution_map_sha256,
        model_execution_map_entry_sha256=hashlib.sha256(
            _canonical_bytes(execution)
        ).hexdigest(),
    )
    if list(eligibility_validator.iter_errors(eligibility)):
        raise ModelNotEligible("scheduler_eligibility_receipt_schema_invalid")
    eligibility_raw = _pretty_bytes(eligibility)
    scheduler_digest = hashlib.sha256(eligibility_raw).hexdigest()
    eligibility_path = _eligibility_path(owner, scheduler_digest)

    if state == "qualified":
        if (
            qualification.get("public_completion_receipt_sha256") != public_digest
            or qualification.get("scheduler_eligibility_receipt_sha256")
            != scheduler_digest
            or qualification.get("qualified_at") != qualified_at
        ):
            raise ModelNotEligible("already_qualified_with_other_evidence")
        if (
            existing_eligibility != eligibility
            or existing_eligibility_raw != eligibility_raw
        ):
            raise ModelNotEligible("qualified_scheduler_receipt_mismatch")
        return (
            ModelDecision(
                model_id,
                "unchanged",
                "already_qualified",
                public_digest,
                scheduler_digest,
                qualified_at,
            ),
            None,
            None,
            None,
        )
    if state != "active":
        raise ModelNotEligible("profile_state_not_promotable")
    promoted = copy.deepcopy(profile)
    _promote_profile(
        promoted,
        public_digest=public_digest,
        scheduler_digest=scheduler_digest,
        qualified_at=qualified_at,
    )
    if list(profile_validator.iter_errors(promoted)):
        raise ModelNotEligible("promoted_profile_schema_invalid")
    return (
        ModelDecision(
            model_id,
            "promote",
            "exact_acceptance_and_scheduler_evidence",
            public_digest,
            scheduler_digest,
            qualified_at,
        ),
        promoted,
        eligibility_raw,
        eligibility_path,
    )


def _stage_payload(path: Path, payload: bytes, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.promotion.", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _atomic_write(payloads: dict[Path, bytes]) -> None:
    """Replace all projections together and restore every old byte on failure."""

    staged: dict[Path, Path] = {}
    backups: dict[Path, tuple[Path, int]] = {}
    replaced: list[Path] = []
    created: set[Path] = set()
    retained: set[Path] = set()
    try:
        for path, payload in payloads.items():
            if path.is_symlink():
                raise PromotionError("promotion_target_invalid")
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            staged[path] = _stage_payload(path, payload, mode)
            if path.exists():
                backup = _stage_payload(path, path.read_bytes(), mode)
                backups[path] = (backup, mode)
            else:
                created.add(path)
        try:
            for path, temporary_path in staged.items():
                os.replace(temporary_path, path)
                replaced.append(path)
        except BaseException as replacement_error:
            rollback_errors: list[str] = []
            for path in reversed(replaced):
                if path in created:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as rollback_error:
                        rollback_errors.append(f"{path}: {rollback_error}")
                    continue
                backup, _ = backups[path]
                try:
                    os.replace(backup, path)
                except OSError as rollback_error:
                    retained.add(backup)
                    rollback_errors.append(f"{path}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    "promotion update failed and rollback was incomplete; backups retained"
                ) from replacement_error
            raise
    finally:
        for temporary_path in (
            *staged.values(),
            *(item[0] for item in backups.values()),
        ):
            if temporary_path not in retained:
                temporary_path.unlink(missing_ok=True)


def promote(
    *,
    repository_root: Path,
    aggregate_path: Path,
    acceptance_repository_root: Path | None = None,
    write: bool = False,
) -> PromotionResult:
    """Plan or atomically apply every evidence-matching model promotion."""

    try:
        root = repository_root.resolve(strict=True)
    except OSError as error:
        raise PromotionError("repository_root_invalid") from error
    if not root.is_dir():
        raise PromotionError("repository_root_invalid")
    (
        profile_path,
        profiles_document,
        profiles,
        executions,
        owners,
        execution_map_sha256,
    ) = _load_repository(root)
    if acceptance_repository_root is None:
        acceptance_root = root
    else:
        try:
            acceptance_root = acceptance_repository_root.resolve(strict=True)
        except OSError as error:
            raise PromotionError("acceptance_repository_root_invalid") from error
        if not acceptance_root.is_dir():
            raise PromotionError("acceptance_repository_root_invalid")
    if acceptance_root == root:
        acceptance_profiles = profiles
        acceptance_executions = executions
        acceptance_owners = owners
        acceptance_execution_map_sha256 = execution_map_sha256
    else:
        (
            _acceptance_profile_path,
            _acceptance_profiles_document,
            acceptance_profiles,
            acceptance_executions,
            acceptance_owners,
            acceptance_execution_map_sha256,
        ) = _load_repository(acceptance_root)
        if (
            set(acceptance_profiles) != set(profiles)
            or set(acceptance_executions) != set(executions)
            or set(acceptance_owners) != set(owners)
        ):
            raise PromotionError("acceptance_repository_model_set_invalid")
    aggregate, aggregate_raw, rows = _validate_aggregate(
        aggregate_path,
        profiles=profiles,
        owners=owners,
    )
    aggregate_digest = hashlib.sha256(aggregate_raw).hexdigest()
    schema, _ = _read_json(root / PROFILE_SCHEMA_RELATIVE, "profile_schema_unavailable")
    profile_validator = Draft202012Validator(schema, format_checker=FormatChecker())
    eligibility_schema, _ = _read_json(
        root / ELIGIBILITY_SCHEMA_RELATIVE, "eligibility_schema_unavailable"
    )
    eligibility_validator = Draft202012Validator(
        eligibility_schema, format_checker=FormatChecker()
    )
    decisions: list[ModelDecision] = []
    promoted_profiles: dict[str, dict[str, Any]] = {}
    eligibility_payloads: dict[Path, bytes] = {}
    for model_id in sorted(rows):
        try:
            decision, promoted_profile, eligibility_raw, eligibility_path = (
                _evaluate_model(
                    model_id=model_id,
                    row=rows[model_id],
                    aggregate=aggregate,
                    aggregate_digest=aggregate_digest,
                    aggregate_directory=aggregate_path.resolve().parent,
                    profile=profiles[model_id],
                    execution=executions[model_id],
                    owner=owners[model_id],
                    acceptance_profile=acceptance_profiles[model_id],
                    acceptance_execution=acceptance_executions[model_id],
                    acceptance_owner=acceptance_owners[model_id],
                    acceptance_execution_map_sha256=(acceptance_execution_map_sha256),
                    profile_validator=profile_validator,
                    eligibility_validator=eligibility_validator,
                )
            )
        except ModelNotEligible as error:
            decision = ModelDecision(model_id, "skip", error.code)
            promoted_profile = None
            eligibility_raw = None
            eligibility_path = None
        decisions.append(decision)
        if promoted_profile is not None:
            promoted_profiles[model_id] = promoted_profile
            assert eligibility_raw is not None and eligibility_path is not None
            eligibility_payloads[eligibility_path] = eligibility_raw

    payloads: dict[Path, bytes] = {}
    if promoted_profiles:
        promoted_catalog = copy.deepcopy(profiles_document)
        for index, profile in enumerate(promoted_catalog["profiles"]):
            model_id = profile["model_id"]
            if model_id in promoted_profiles:
                promoted_catalog["profiles"][index] = copy.deepcopy(
                    promoted_profiles[model_id]
                )
        payloads[profile_path] = _pretty_bytes(promoted_catalog)
        for model_id, profile in promoted_profiles.items():
            owner = owners[model_id]
            payloads[owner.path] = _pretty_bytes(_promote_owner(owner, profile))
        payloads.update(eligibility_payloads)
    if write and payloads:
        _atomic_write(payloads)
    return PromotionResult(
        aggregate_sha256=aggregate_digest,
        execution_map_sha256=execution_map_sha256,
        acceptance_execution_map_sha256=acceptance_execution_map_sha256,
        decisions=tuple(decisions),
        written_paths=tuple(sorted(payloads)) if write else (),
    )


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate", required=True, type=Path)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--acceptance-repository-root",
        type=Path,
        help=(
            "read-only checkout that supplied the aggregate inputs; use only "
            "to bridge unrelated fleet execution-map digest refreshes"
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="atomically update matching profiles; the default is a read-only plan",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = _arguments(argv)
    try:
        result = promote(
            repository_root=options.repository_root,
            aggregate_path=options.aggregate,
            acceptance_repository_root=options.acceptance_repository_root,
            write=options.write,
        )
    except PromotionError as error:
        print(
            json.dumps({"status": "failed", "error_code": error.code}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": "written" if options.write else "planned",
                "aggregate_sha256": result.aggregate_sha256,
                "execution_map_sha256": result.execution_map_sha256,
                "acceptance_execution_map_sha256": (
                    result.acceptance_execution_map_sha256
                ),
                "promoted": result.promoted,
                "unchanged": result.unchanged,
                "skipped": result.skipped,
                "models": [
                    {
                        "model_id": item.model_id,
                        "action": item.action,
                        "reason": item.reason,
                        "public_completion_receipt_sha256": item.public_completion_receipt_sha256,
                        "scheduler_eligibility_receipt_sha256": item.scheduler_eligibility_receipt_sha256,
                        "qualified_at": item.qualified_at,
                    }
                    for item in result.decisions
                ],
                "written_paths": [str(path) for path in result.written_paths],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
