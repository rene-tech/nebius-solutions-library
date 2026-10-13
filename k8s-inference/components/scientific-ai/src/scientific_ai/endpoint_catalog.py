"""Immutable, source-backed endpoint contracts; entries never enable deployments.

The snapshot contains public schemas and synthetic examples only. Runtime image,
native binding, authorization, and readiness must come from authenticated S2
discovery. Historical schema descriptions do not confer durable execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

_TRANSPORT = "x-fs2-artifact-materialization"
_LEGACY_ASYNC = " Submit once, then poll the returned operation ID and retrieve its result when completed."


def _project(value: Any, *, surface: str, model_id: str) -> Any:
    """Preserve runtime fields; separate artifact transport from native values."""
    if isinstance(value, list):
        return [_project(item, surface=surface, model_id=model_id) for item in value]
    if not isinstance(value, dict):
        return value
    if _TRANSPORT in value:
        choices = value["anyOf"]
        if surface == "native":
            return _project(choices[0], surface=surface, model_id=model_id)
        # Pinned legacy fixture bytes are not part of this application package.
        choices = [item for item in choices if "fixture_id" not in item.get("properties", {})]
        if surface == "mcp" and (value[_TRANSPORT] == "base64" or model_id == "altumage"):
            choices = [item for item in choices if "artifact_id" in item.get("properties", {})]
        value = {**value, "anyOf": choices}
    return {
        key: (
            item.removesuffix(_LEGACY_ASYNC)
            if key == "description" and isinstance(item, str)
            else _project(item, surface=surface, model_id=model_id)
        )
        for key, item in value.items()
    }


def _has_fixture(value: Any) -> bool:
    if isinstance(value, dict):
        return "fixture_id" in value or any(_has_fixture(item) for item in value.values())
    return isinstance(value, list) and any(_has_fixture(item) for item in value)


def _local_references(value: Any) -> None:
    if isinstance(value, dict):
        if "$ref" in value and not value["$ref"].startswith("#/"):
            raise ValueError("endpoint schemas may only contain local references")
        for item in value.values():
            _local_references(item)
    elif isinstance(value, list):
        for item in value:
            _local_references(item)


@dataclass(frozen=True)
class EndpointContract:
    model_id: str
    model_ref: str
    protocol: str
    source_refs: tuple[str, ...]
    tool_name: str | None
    tool_name_unavailable: str | None
    tool_name_provenance: str
    tool_name_source_refs: tuple[str, ...]
    _schema_json: str = field(repr=False)
    _examples_json: str = field(repr=False)

    @property
    def source_schema_sha256(self) -> str:
        return hashlib.sha256(self._schema_json.encode()).hexdigest()

    def schema(self, surface: str = "http") -> dict:
        if surface not in {"http", "mcp", "native"}:
            raise ValueError("unknown endpoint contract surface")
        return _project(json.loads(self._schema_json), surface=surface, model_id=self.model_id)

    @property
    def examples(self) -> list[dict]:
        # Do not advertise unavailable packaged fixtures as working examples.
        return [item for item in json.loads(self._examples_json) if not _has_fixture(item)]

    def describe(self) -> dict:
        return {
            "model_id": self.model_id,
            "model_ref": self.model_ref,
            "protocol": self.protocol,
            "tool_name": self.tool_name,
            "tool_name_unavailable": self.tool_name_unavailable,
            "tool_name_provenance": self.tool_name_provenance,
            "tool_name_source_refs": list(self.tool_name_source_refs),
            "input_schema": self.schema(),
            "mcp_input_schema": self.schema("mcp"),
            "examples": self.examples,
            "source_refs": list(self.source_refs),
            "source_schema_sha256": self.source_schema_sha256,
            "runtime_identity": None,
            "readiness": "unknown",
            "execution": {
                "mode": "synchronous-native-binding",
                "durable_async": False,
                "idempotency": False,
                "pinned_input_fixtures": False,
            },
        }


@lru_cache(maxsize=1)
def catalog():
    snapshot = json.loads(files("scientific_ai").joinpath("endpoint_schemas.json").read_text())
    result = {}
    for model_id, record in snapshot["contracts"].items():
        schema = record["input_schema"]
        _local_references(schema)
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object" or not schema.get("properties"):
            raise ValueError("endpoint contract must have meaningful model fields")
        result[model_id] = EndpointContract(
            model_id=model_id,
            model_ref=record["model_ref"],
            protocol=record["protocol"],
            source_refs=tuple(record["source_refs"]),
            tool_name=record.get("tool_name"),
            tool_name_unavailable=record.get("tool_name_unavailable"),
            tool_name_provenance=record["tool_name_provenance"],
            tool_name_source_refs=tuple(record["tool_name_source_refs"]),
            _schema_json=json.dumps(schema, sort_keys=True, separators=(",", ":")),
            _examples_json=json.dumps(record["examples"], separators=(",", ":")),
        )
    return MappingProxyType(result)


def contract_for(model_id: str) -> EndpointContract:
    """Exact public App identity only; no aliases, substring matches or fallback."""
    try:
        return catalog()[model_id]
    except KeyError:
        raise KeyError("endpoint contract is unavailable") from None
