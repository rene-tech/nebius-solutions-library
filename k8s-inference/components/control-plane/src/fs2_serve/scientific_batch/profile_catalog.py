"""Canonical public-schema consumer for scientific workload profiles.

The controller owns no copy of the request, result, artifact, or profile JSON
schemas. It loads and validates the catalog files shipped in the runtime image,
then projects their operator-owned workload subset into internal records.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

SCIENTIFIC_REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
SCIENTIFIC_RESULT_SCHEMA = "fs2-serve.nebius.ai/scientific-run-result/v1"
SCIENTIFIC_ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"


class ScientificProfileError(RuntimeError):
    """Canonical scientific catalog state is unavailable or inconsistent."""


class ScientificRequestError(ValueError):
    """A public request does not satisfy the canonical catalog contracts."""


def _object_schema(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ScientificProfileError(f"{label} is not an object")
    return cast(Mapping[str, Any], value)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            raise ScientificProfileError(f"{label} exceeds the catalog bound")
        value = json.loads(raw)
    except (OSError, RecursionError, ValueError) as error:
        raise ScientificProfileError(f"{label} is unavailable or invalid") from error
    if not isinstance(value, dict):
        raise ScientificProfileError(f"{label} is not an object")
    return cast(dict[str, Any], value)


def _validator(schema: Mapping[str, Any], label: str) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())
    except SchemaError as error:
        raise ScientificProfileError(f"{label} is not a valid JSON Schema") from error


def _schema_contract_name(schema: Mapping[str, Any]) -> str | None:
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        identity = properties.get("schema")
        if isinstance(identity, Mapping) and isinstance(identity.get("const"), str):
            return cast(str, identity["const"])
    identifier = schema.get("$id")
    if isinstance(identifier, str) and "/schema/" in identifier:
        name, _, version = identifier.rpartition("/")
        slug = name.rpartition("/")[2]
        if slug and version.startswith("v"):
            return f"fs2-serve.nebius.ai/{slug}/{version}"
    return None


@dataclass(frozen=True, slots=True)
class ScientificWorkloadProfile:
    """One schema-validated public catalog profile, not a controller schema."""

    value: Mapping[str, Any]

    @property
    def model_id(self) -> str:
        return cast(str, self.value["model_id"])

    @property
    def model_revision(self) -> str:
        return cast(str, cast(Mapping[str, Any], self.value["execution_identity"])["model_revision"])

    @property
    def operations(self) -> tuple[str, ...]:
        interface = cast(Mapping[str, Any], self.value["interface"])
        return tuple(cast(list[str], interface["operations"]))

    @property
    def service_classes(self) -> tuple[str, ...]:
        interface = cast(Mapping[str, Any], self.value["interface"])
        return tuple(cast(list[str], interface["service_classes"]))

    @property
    def parameter_schema(self) -> str:
        interface = cast(Mapping[str, Any], self.value["interface"])
        return cast(str, interface["parameter_schema"])

    @property
    def display_name(self) -> str:
        return cast(str, self.value["display_name"])

    @property
    def execution_mode(self) -> str:
        return cast(str, self.value["execution_mode"])

    @property
    def source_repository(self) -> str:
        source = cast(Mapping[str, Any], self.value["source"])
        return cast(str, source["repository"])

    @property
    def runtime_image_digest(self) -> str:
        identity = cast(Mapping[str, Any], self.value["execution_identity"])
        return cast(str, identity["runtime_image_digest"])

    @property
    def execution_identity_sha256(self) -> str:
        identity = cast(Mapping[str, Any], self.value["execution_identity"])
        return cast(str, identity["execution_identity_sha256"])

    @property
    def access_profile(self) -> str:
        access = cast(Mapping[str, Any], self.value["access"])
        return cast(str, access["profile"])

    @property
    def access_state(self) -> str:
        access = cast(Mapping[str, Any], self.value["access"])
        return cast(str, access["state"])

    @property
    def access_receipt_digest(self) -> str | None:
        access = cast(Mapping[str, Any], self.value["access"])
        return cast(str | None, access["receipt_digest"])

    @property
    def mcp_discoverable(self) -> bool:
        interface = cast(Mapping[str, Any], self.value["interface"])
        mcp = cast(Mapping[str, Any], interface["mcp"])
        return mcp.get("discoverable") is True

    @property
    def mcp_tool_name(self) -> str:
        interface = cast(Mapping[str, Any], self.value["interface"])
        mcp = cast(Mapping[str, Any], interface["mcp"])
        return cast(str, mcp["tool_name"])

    @property
    def mcp_description(self) -> str:
        interface = cast(Mapping[str, Any], self.value["interface"])
        mcp = cast(Mapping[str, Any], interface["mcp"])
        return cast(str, mcp["description"])

    @property
    def mcp_invocable(self) -> bool:
        interface = cast(Mapping[str, Any], self.value["interface"])
        mcp = cast(Mapping[str, Any], interface["mcp"])
        return mcp.get("invocable") is True

    @property
    def runnable(self) -> bool:
        identity = cast(Mapping[str, Any], self.value["execution_identity"])
        access = cast(Mapping[str, Any], self.value["access"])
        semantic = cast(Mapping[str, Any], self.value["semantic_validation"])
        return (
            self.value.get("route_exposed") is True
            and self.value.get("state") in {"qualified", "active"}
            and access.get("state") in {"not-required", "verified"}
            and semantic.get("state") in {"qualified", "active"}
            and all(
                identity.get(field) is not None
                for field in (
                    "model_revision",
                    "runtime_image_digest",
                    "runtime_recipe_sha256",
                    "workload_recipe_sha256",
                    "artifact_manifest_digest",
                    "execution_identity_sha256",
                )
            )
        )


class ScientificProfileCatalog:
    """Validated catalog plus canonical request/parameter validators."""

    def __init__(
        self,
        *,
        profiles: Mapping[str, ScientificWorkloadProfile],
        validators: Mapping[str, Draft202012Validator],
    ) -> None:
        self._profiles = MappingProxyType(dict(profiles))
        self._validators = MappingProxyType(dict(validators))
        if SCIENTIFIC_REQUEST_SCHEMA not in self._validators or SCIENTIFIC_RESULT_SCHEMA not in self._validators:
            raise ScientificProfileError("canonical scientific request and result schemas are required")
        result_schema = _object_schema(self._validators[SCIENTIFIC_RESULT_SCHEMA].schema, "scientific result schema")
        properties = _object_schema(result_schema.get("properties"), "scientific result properties")
        attempts = _object_schema(properties.get("attempts"), "scientific result attempts")
        maximum = attempts.get("maxItems")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 1_000_000:
            raise ScientificProfileError("canonical scientific result attempt bound is invalid")
        self.max_result_attempts = maximum

    @classmethod
    def load(cls, catalog_dir: Path) -> ScientificProfileCatalog:
        schema_dir = catalog_dir / "schema"
        profile_set = _read_object(
            catalog_dir / "contracts/scientific-workload-profiles.json",
            "scientific workload profile set",
        )
        set_schema = _read_object(
            schema_dir / "scientific-workload-profiles.schema.json",
            "scientific workload profile-set schema",
        )
        profile_schema = _read_object(
            schema_dir / "scientific-workload-profile.schema.json",
            "scientific workload profile schema",
        )
        try:
            _validator(set_schema, "scientific workload profile-set schema").validate(profile_set)
        except ValidationError as error:
            raise ScientificProfileError(
                "scientific workload profile set does not satisfy its canonical schema"
            ) from error

        validators: dict[str, Draft202012Validator] = {}
        for path in sorted(catalog_dir.rglob("*.schema.json")):
            schema = _read_object(path, f"catalog schema {path.name}")
            contract = _schema_contract_name(schema)
            if contract is not None:
                validators[contract] = _validator(schema, f"catalog schema {path.name}")

        profile_validator = _validator(profile_schema, "scientific workload profile schema")
        profiles: dict[str, ScientificWorkloadProfile] = {}
        for raw in profile_set.get("profiles", []):
            try:
                profile_validator.validate(raw)
            except ValidationError as error:
                raise ScientificProfileError("a scientific workload profile violates its canonical schema") from error
            profile = ScientificWorkloadProfile(MappingProxyType(cast(dict[str, Any], raw)))
            if profile.model_id in profiles:
                raise ScientificProfileError("scientific workload profile model IDs must be unique")
            profiles[profile.model_id] = profile
        return cls(profiles=profiles, validators=validators)

    def list(self, *, runnable_only: bool = True) -> tuple[ScientificWorkloadProfile, ...]:
        profiles = tuple(self._profiles[key] for key in sorted(self._profiles))
        return tuple(profile for profile in profiles if profile.runnable) if runnable_only else profiles

    def get(self, model_id: str, *, runnable: bool = True) -> ScientificWorkloadProfile:
        profile = self._profiles.get(model_id)
        if profile is None or (runnable and not profile.runnable):
            raise ScientificProfileError("scientific workload profile is not runnable")
        return profile

    def validate_request(self, profile: ScientificWorkloadProfile, value: object) -> dict[str, Any]:
        try:
            self._validators[SCIENTIFIC_REQUEST_SCHEMA].validate(value)
            if not isinstance(value, dict):  # implied by schema, retained for type narrowing
                raise ScientificRequestError("scientific run request must be an object")
            if value["operation"] not in profile.operations:
                raise ScientificRequestError("operation is not supported by the workload profile")
            if value["service_class"] not in profile.service_classes:
                raise ScientificRequestError("service class is not supported by the workload profile")
            parameter_validator = self._validators.get(profile.parameter_schema)
            if parameter_validator is None:
                raise ScientificProfileError("profile parameter schema is absent from the canonical catalog")
            parameter_validator.validate(value["parameters"])
        except ValidationError as error:
            raise ScientificRequestError("scientific run request violates a canonical schema") from error
        return cast(dict[str, Any], value)

    def validate_result(self, value: object) -> None:
        try:
            self._validators[SCIENTIFIC_RESULT_SCHEMA].validate(value)
        except ValidationError as error:
            raise ScientificProfileError("scientific result violates its canonical schema") from error

    def validate_artifact_manifest(self, value: object) -> Mapping[str, Any]:
        """Validate and return the existing catalog-owned manifest contract."""

        try:
            validator = self._validators.get(SCIENTIFIC_ARTIFACT_MANIFEST_SCHEMA)
            if validator is None:
                raise ScientificProfileError("canonical scientific artifact manifest schema is absent")
            validator.validate(value)
        except ValidationError as error:
            raise ScientificRequestError("input manifest violates the canonical artifact schema") from error
        if not isinstance(value, Mapping):  # implied by schema; narrows the return type
            raise ScientificRequestError("input manifest is not an object")
        return cast(Mapping[str, Any], value)
