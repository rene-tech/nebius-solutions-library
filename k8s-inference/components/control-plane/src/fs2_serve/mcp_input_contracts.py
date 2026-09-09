"""Publish and enforce model JSON Schemas without generating Python signatures.

The pinned MCP SDK derives schemas from Python functions by default. Our model
contracts already exist as JSON Schema (including nested scientific inputs), so
this small SDK adapter replaces both the published parameters and argument
validation together. The original authorized admission handler and result
conversion remain unchanged. Missing optional fields stay missing; scientific
inputs are not coerced, synthesized, or silently dropped.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Literal

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError as SchemaValidationError  # type: ignore[import-untyped]
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from .models import MAX_IDEMPOTENCY_KEY_LENGTH, MIN_IDEMPOTENCY_KEY_LENGTH


def describe_tool_parameters(server: MCPServer, name: str, descriptions: dict[str, str]) -> None:
    """Attach human guidance to existing SDK-derived core parameter types."""
    tool = server._tool_manager.get_tool(name)
    if tool is None:
        raise ValueError("register the core tool before documenting its parameters")
    for field, schema in tool.parameters.get("properties", {}).items():
        if field in descriptions:
            schema["description"] = descriptions[field]


def tool_input_schema(payload_schema: dict[str, Any], *, scientific: bool, max_wait_seconds: float) -> dict[str, Any]:
    """Add only existing gateway controls to a concrete model/run schema."""
    schema = deepcopy(payload_schema)
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise ValueError("model input contract must describe an object with concrete properties")
    controls: dict[str, Any] = {
        "idempotency_key": {
            "type": ["string", "null"],
            "minLength": MIN_IDEMPOTENCY_KEY_LENGTH,
            "maxLength": MAX_IDEMPOTENCY_KEY_LENGTH,
            "description": "Reuse this key when retrying the same submission to avoid duplicate work.",
        },
    }
    if not scientific:
        controls["wait_seconds"] = {
            "type": "number",
            "minimum": 0,
            "maximum": max_wait_seconds,
            "default": 0,
            "description": "Wait briefly for completion; otherwise poll get_operation using the returned operation ID.",
        }
    if set(schema["properties"]) & set(controls):
        raise ValueError("model fields collide with reserved gateway submission controls")
    schema["properties"].update(controls)
    schema["additionalProperties"] = False
    Draft202012Validator.check_schema(schema)
    return schema


def _issue(error: SchemaValidationError) -> dict[str, Any]:
    # jsonschema's default message includes rejected values. Provide locations
    # and schema expectations instead; full inputs belong in encrypted debug
    # capture, not SDK exception text or stdout.
    field = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path)
    result: dict[str, Any] = {"field": field, "rule": str(error.validator)}
    if error.validator == "required" and isinstance(error.instance, dict):
        result["missing_fields"] = [name for name in error.validator_value if name not in error.instance]
    elif error.validator in {
        "type",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
    }:
        result["expected"] = error.validator_value
    elif error.validator == "additionalProperties":
        result["message"] = "Only the fields declared by this runtime's input schema are supported."
        result["allowed_fields"] = sorted(error.schema.get("properties", {}))
    else:
        result["message"] = "Input does not match the declared model schema."
    return result


class ContractFuncMetadata(FuncMetadata):
    """SDK argument adapter preserving raw JSON values and optional-field absence."""

    model_id: str
    input_schema: dict[str, Any]
    payload_key: Literal["payload", "request"]
    openai: bool = False

    def validate_arguments(self, arguments_to_validate: dict[str, Any]) -> dict[str, Any]:
        controls = {"idempotency_key"} | ({"wait_seconds"} if self.payload_key == "payload" else set())
        incoming = deepcopy(arguments_to_validate)
        # Keep old clients working, but never publish an opaque wrapper as the
        # model's new tool schema. Validate wrapped inputs against the same
        # selected-runtime schema, not a more permissive fallback.
        legacy = self.payload_key in incoming and set(incoming) <= controls | {self.payload_key}
        if legacy and isinstance(incoming[self.payload_key], dict):
            payload = incoming.pop(self.payload_key)
            if self.openai:
                # Admission already binds the qualified upstream model name.
                # The historical wrapper's model field never selected a route.
                payload.pop("model", None)
            if set(payload) & controls:
                raise MCPError(code=INVALID_PARAMS, message="Submission controls belong outside model inputs")
            incoming = payload | incoming
        errors = []
        for error in Draft202012Validator(self.input_schema).iter_errors(incoming):
            errors.append(_issue(error))
            if len(errors) == 12:
                break
        if "wait_seconds" in incoming and isinstance(incoming["wait_seconds"], float | int):
            if not math.isfinite(incoming["wait_seconds"]):
                errors.append({"field": "/wait_seconds", "rule": "finite", "message": "Use a finite wait duration."})
        if errors:
            raise MCPError(
                code=INVALID_PARAMS,
                message=f"Invalid inputs for {self.model_id}. Follow this tool's inputSchema or call get_model_schema.",
                data={"type": "model_input_validation", "model_id": self.model_id, "issues": errors},
            )
        return {
            self.payload_key: {key: value for key, value in incoming.items() if key not in controls},
            **{key: incoming[key] for key in controls if key in incoming},
        }


def apply_tool_input_contract(
    server: MCPServer,
    name: str,
    *,
    model_id: str,
    payload_schema: dict[str, Any],
    scientific: bool,
    max_wait_seconds: float,
    openai: bool = False,
) -> None:
    """Keep the SDK's advertised schema and executable validation identical.

    Access to the pinned SDK's registration object is isolated here and covered
    by real SDK list/call tests, including nested refs and legacy clients.
    """
    tool = server._tool_manager.get_tool(name)
    if tool is None:
        raise ValueError("register the authorized tool handler before its input contract")
    schema = tool_input_schema(payload_schema, scientific=scientific, max_wait_seconds=max_wait_seconds)
    previous = tool.fn_metadata
    tool.fn_metadata = ContractFuncMetadata(
        arg_model=previous.arg_model,
        output_schema=previous.output_schema,
        output_model=previous.output_model,
        wrap_output=previous.wrap_output,
        model_id=model_id,
        input_schema=schema,
        payload_key="request" if scientific else "payload",
        openai=openai,
    )
    tool.parameters = schema
