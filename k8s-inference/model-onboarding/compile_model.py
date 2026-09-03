#!/usr/bin/env python3
"""Compile one model declaration into deterministic onboarding projections.

The compiler deliberately writes only to an operator-selected staging directory.
It does not edit the canonical catalog, Terraform inputs, state, or Kubernetes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
except ImportError as error:
    # Exercised only outside the repository's catalog test environment.
    raise SystemExit(
        "model onboarding requires jsonschema; run it from the catalog test environment"
    ) from error


COMPILER_SCHEMA = "fs2-serve.nebius.ai/model-onboarding-bundle/v2"
DECLARATION_SCHEMA = Path(__file__).with_name("model-declaration.schema.json")
DEFAULT_SOLUTION_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH_TOKEN = "{MODEL_PATH}"
CATALOG_MODEL_PATH_TOKEN = "{FS2_MODEL_CONTENT_PATH}"
_IMAGE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[1-9][0-9]{0,4})?/)"
    r"(?:[a-z0-9]+(?:[._-]+[a-z0-9]+)*)(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*"
    r"@(sha256:[0-9a-f]{64})$"
)
_LOGICAL_REPOSITORY = re.compile(
    r"^[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)+$"
)
_HF_REPO_ID = re.compile(
    r"^(?=.{3,96}$)[A-Za-z0-9_][A-Za-z0-9._-]*/[A-Za-z0-9_][A-Za-z0-9._-]*$"
)
_LABEL_NAME = re.compile(r"^[A-Za-z0-9](?:[-_.A-Za-z0-9]{0,61}[A-Za-z0-9])?$")
_LABEL_VALUE = _LABEL_NAME
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_SENSITIVE_ENV = re.compile(r"(?:TOKEN|PASSWORD|SECRET|CREDENTIAL|API_KEY|PRIVATE_KEY)")
_RESERVED_ENV = {
    "FS2_MODEL_ID",
    "FS2_MODEL_REPOSITORY",
    "FS2_MODEL_REVISION",
    "HF_HOME",
    "HF_HUB_DISABLE_TELEMETRY",
    "HOME",
}
_CANONICAL_PROTOCOL_PATHS = {
    "openai-chat": "/v1/chat/completions",
    "openai-completions": "/v1/completions",
    "openai-embeddings": "/v1/embeddings",
    "openai-images": "/v1/images/generations",
}
_PROMOTION_GAPS = (
    "add the model to model-accelerator-compatibility.json with reviewed hardware evidence",
    "add artifact acquisition, provenance-lock, semantic-request, and runtime-prerequisite contracts",
    "add a two-request semantic validator and fixture, then replace the blocked projection",
    "refresh scale contracts and golden identities with the existing catalog scripts",
    "run the existing catalog loader and all-model live-release checks",
    "record retained cold-start, HTTP, MCP, and elasticity qualification evidence",
)
_BINARY_UNITS = {
    "Ki": 1 << 10,
    "Mi": 1 << 20,
    "Gi": 1 << 30,
    "Ti": 1 << 40,
}
_SCIENTIFIC_ADAPTER_SHARED_RECIPE_PATHS = (
    "components/control-plane/src/fs2_serve/scientific_batch/__init__.py",
    "components/control-plane/src/fs2_serve/scientific_batch/codec.py",
    "components/control-plane/src/fs2_serve/scientific_batch/controller.py",
    "components/control-plane/src/fs2_serve/scientific_batch/execution.py",
    "components/control-plane/src/fs2_serve/scientific_batch/execution_map_builder.py",
    "components/control-plane/src/fs2_serve/scientific_batch/models.py",
    "components/control-plane/src/fs2_serve/scientific_batch/catalog_adapter.py",
    "components/control-plane/src/fs2_serve/scientific_batch/protocols.py",
    "components/control-plane/src/fs2_serve/scientific_batch/scheduling.py",
    "components/control-plane/src/fs2_serve/scientific_batch/service.py",
    "components/control-plane/src/fs2_serve/scientific_batch/adapters/__init__.py",
    "components/control-plane/src/fs2_serve/scientific_batch/adapters/common.py",
    "components/control-plane/src/fs2_serve/scientific_batch/adapters/secondary_structure.py",
    "catalog/runtime/schema/scientific-run-request.schema.json",
    "catalog/runtime/schema/scientific-run-result.schema.json",
    "catalog/runtime/schema/scientific-execution-targets.schema.json",
    "catalog/runtime/schema/scientific-runtime-localizations.schema.json",
    "catalog/runtime/schema/scientific-workload-profile.schema.json",
    "catalog/runtime/contracts/scientific-execution-targets.json",
    "model-onboarding/compile_model.py",
    "model-onboarding/model-declaration.schema.json",
)
_SCIENTIFIC_ADAPTER_MODEL_RECIPE_PATHS = {
    "alphafold3": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/alphafold3.py",
        "model-onboarding/declarations/cancer-immunotherapy/alphafold3.json",
        "models/structure/runtime/alphafold3/adapter.py",
    ),
    "boltzgen": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/boltzgen.py",
        "catalog/runtime/schema/boltzgen-parameters.schema.json",
        "models/structure/batch-adapters/boltzgen/adapter.py",
        "models/structure/batch-adapters/boltzgen/contract.json",
    ),
    "proteina-complexa": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/proteina_complexa.py",
        "catalog/runtime/schema/proteina-complexa-parameters.schema.json",
        "models/structure/batch-adapters/proteina-complexa/adapter.py",
        "models/structure/batch-adapters/proteina-complexa/contract.json",
    ),
    "esmfold2": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/esmfold2.py",
        "model-onboarding/declarations/cancer-immunotherapy/esmfold2.json",
        "models/structure/runtime/esmfold2/adapter.py",
    ),
    "esmfold2-fast": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/esmfold2_fast.py",
        "model-onboarding/declarations/cancer-immunotherapy/esmfold2-fast.json",
        "models/structure/runtime/esmfold2_fast/adapter.py",
    ),
    "openfold3": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/openfold3.py",
        "model-onboarding/declarations/cancer-immunotherapy/openfold3.json",
        "models/structure/runtime/openfold3/adapter.py",
    ),
    "protenix-v2": (
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/protenix_v2.py",
        "model-onboarding/declarations/cancer-immunotherapy/protenix-v2.json",
        "models/structure/runtime/protenix_v2/adapter.py",
    ),
}
_SCIENTIFIC_ADAPTER_PARAMETER_SCHEMA_IDS = {
    "boltzgen": "fs2-serve.nebius.ai/boltzgen-parameters/v1",
    "proteina-complexa": "fs2-serve.nebius.ai/proteina-complexa-parameters/v1",
}


class OnboardingError(RuntimeError):
    """A deterministic, user-actionable onboarding failure."""


@dataclass(frozen=True)
class Artifact:
    """One staged output and its eventual canonical target."""

    path: str
    target: str
    payload: bytes
    kind: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


def _has_http(declaration: Mapping[str, Any]) -> bool:
    return declaration["execution_mode"] in {"http", "hybrid"}


def _has_scientific_batch(declaration: Mapping[str, Any]) -> bool:
    return declaration["execution_mode"] in {"scientific-batch", "hybrid"}


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise OnboardingError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise OnboardingError(f"cannot read {path}: {error}") from error
    try:
        value = json.loads(raw, object_pairs_hook=_pairs_without_duplicates)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise OnboardingError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise OnboardingError(f"{path} must contain one JSON object")
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _scientific_adapter_runtime_recipe_sha256(
    solution_root: Path, model_id: str
) -> str | None:
    """Bind a registered controller adapter to every executable contract input."""

    model_paths = _SCIENTIFIC_ADAPTER_MODEL_RECIPE_PATHS.get(model_id)
    if model_paths is None:
        return None
    digest = hashlib.sha256()
    for relative in sorted((*_SCIENTIFIC_ADAPTER_SHARED_RECIPE_PATHS, *model_paths)):
        path = solution_root / relative
        if not path.is_file():
            raise OnboardingError(
                f"scientific runtime recipe input is missing: {relative}"
            )
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _error_path(error: Any) -> str:
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}"
        for part in error.absolute_path
    )


def _validate_schema(
    value: Mapping[str, Any], schema: Mapping[str, Any], label: str
) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise OnboardingError(f"invalid {label} schema: {error.message}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: (
            tuple(f"{type(part).__name__}:{part}" for part in error.absolute_path),
            error.message,
        ),
    )
    if errors:
        rendered = "; ".join(
            f"{_error_path(error)}: {error.message}" for error in errors[:12]
        )
        if len(errors) > 12:
            rendered += f"; ... and {len(errors) - 12} more"
        raise OnboardingError(f"{label} validation failed: {rendered}")


def _service_name(declaration: Mapping[str, Any]) -> str:
    serving = declaration["serving"]
    if serving is None:
        raise OnboardingError("a scientific-batch-only declaration has no Service")
    return serving.get("service_name", declaration["model"]["id"])


def _manifest_path(declaration: Mapping[str, Any]) -> str:
    profile = declaration.get("profile", {})
    return profile.get(
        "manifest_path", f"models/generated/{declaration['model']['id']}.yaml"
    )


def _canonical_manifest_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "models"
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != value
        or path.suffix not in {".yaml", ".yml"}
    ):
        raise OnboardingError(
            f"{label} must be a canonical repository-relative model manifest"
        )
    return value


def _strict_argv_option(
    argv: list[str], option: str, *, allow_inline: bool = True
) -> str:
    values: list[tuple[int, str, bool]] = []
    for index, item in enumerate(argv):
        if item == option:
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise OnboardingError(f"vLLM {option} requires one value")
            values.append((index, argv[index + 1], False))
        elif item.startswith(f"{option}="):
            if not allow_inline:
                raise OnboardingError(
                    f"vLLM {option} must use a separate flag and value"
                )
            values.append((index, item.split("=", 1)[1], True))
    if len(values) != 1 or not values[0][1]:
        raise OnboardingError(f"vLLM argv must set {option} exactly once")
    index, value, inline = values[0]
    next_index = index + 1 if inline else index + 2
    if next_index < len(argv) and not argv[next_index].startswith("-"):
        raise OnboardingError(f"vLLM {option} must have exactly one value")
    return value


def _cpu_millis(value: str) -> int:
    return int(value[:-1]) if value.endswith("m") else int(value) * 1000


def _binary_bytes(value: str) -> int:
    unit = value[-2:]
    return int(value[:-2]) * _BINARY_UNITS[unit]


def _resource_projection(resources: Mapping[str, Any]) -> dict[str, int | float]:
    requests = resources["requests"]
    limits = resources["limits"]
    request_values = {
        "cpu": _cpu_millis(requests["cpu"]),
        "memory": _binary_bytes(requests["memory"]),
        "ephemeral_storage": _binary_bytes(requests["ephemeral_storage"]),
    }
    limit_values = {
        "cpu": _cpu_millis(limits["cpu"]),
        "memory": _binary_bytes(limits["memory"]),
        "ephemeral_storage": _binary_bytes(limits["ephemeral_storage"]),
    }
    for resource_name, request in request_values.items():
        if limit_values[resource_name] < request:
            raise OnboardingError(
                f"resources.limits.{resource_name} must be greater than or equal to its request"
            )
    ephemeral_bytes = request_values["ephemeral_storage"]
    gibibyte = 1 << 30
    ephemeral_gib: int | float = (
        ephemeral_bytes // gibibyte
        if ephemeral_bytes % gibibyte == 0
        else ephemeral_bytes / gibibyte
    )
    return {
        "cpu_millis": request_values["cpu"],
        "memory_bytes": request_values["memory"],
        "ephemeral_storage_request_gib": ephemeral_gib,
    }


def _valid_label_key(key: str) -> bool:
    if len(key) > 317 or key.count("/") > 1:
        return False
    if "/" not in key:
        return _LABEL_NAME.fullmatch(key) is not None
    prefix, name = key.split("/", 1)
    if not prefix or len(prefix) > 253 or _LABEL_NAME.fullmatch(name) is None:
        return False
    return all(_DNS_LABEL.fullmatch(part) is not None for part in prefix.split("."))


def _valid_image_reference(value: str) -> bool:
    if _IMAGE.fullmatch(value) is None:
        return False
    registry = value.split("/", 1)[0]
    if ":" in registry:
        host, port = registry.rsplit(":", 1)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            return False
    else:
        host = registry
    return all(_DNS_LABEL.fullmatch(part) is not None for part in host.split("."))


def _runtime_image(runtime: Mapping[str, Any]) -> tuple[str, str | None, str]:
    """Return repository, digest, and supply state without inventing a registry."""

    value = runtime["image"]
    if isinstance(value, str):
        match = _IMAGE.fullmatch(value)
        if match is None:
            raise OnboardingError(
                "runtime.image must be a canonical lowercase OCI digest or a logical batch repository"
            )
        return value.rsplit("@", 1)[0], match.group(1), "digest-pinned"
    if not isinstance(value, Mapping):
        raise OnboardingError("runtime.image must be a string or object")
    repository = value.get("logical_repository")
    digest = value.get("digest")
    state = value.get("state")
    if (
        not isinstance(repository, str)
        or _LOGICAL_REPOSITORY.fullmatch(repository) is None
    ):
        raise OnboardingError(
            "runtime.image.logical_repository must be a provider-neutral repository path"
        )
    first = repository.split("/", 1)[0]
    if "." in first or ":" in first:
        raise OnboardingError(
            "runtime.image.logical_repository cannot contain a registry host"
        )
    if state == "build-required":
        if digest is not None:
            raise OnboardingError(
                "a build-required runtime image cannot declare a digest"
            )
    elif state == "digest-pinned":
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise OnboardingError(
                "a digest-pinned runtime image requires an immutable digest"
            )
    else:
        raise OnboardingError("runtime.image.state is unsupported")
    return repository, digest, state


def _parameter_contract(
    batch: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any] | None, str | None]:
    value = batch["parameter_schema"]
    if isinstance(value, str):
        return value, None, None
    if not isinstance(value, Mapping):
        raise OnboardingError(
            "batch.parameter_schema must be an ID or an inline contract"
        )
    schema_id = value.get("id")
    document = value.get("document")
    if not isinstance(schema_id, str) or not isinstance(document, Mapping):
        raise OnboardingError("batch.parameter_schema inline contract is incomplete")
    try:
        Draft202012Validator.check_schema(document)
    except SchemaError as error:
        raise OnboardingError(
            f"invalid batch parameter schema: {error.message}"
        ) from error
    if (
        document.get("type") != "object"
        or document.get("additionalProperties") is not False
    ):
        raise OnboardingError(
            "batch parameter schema must be a fail-closed object with additionalProperties=false"
        )
    return schema_id, document, hashlib.sha256(_canonical_bytes(document)).hexdigest()


def _deployment_pool_contract(solution_root: Path) -> dict[str, Any]:
    """Load the reviewed retained-deployment pool inventory.

    Scientific declarations express an accelerator requirement. Pool IDs are a
    deployment resolution of that requirement and must never be accepted from
    declaration text alone.
    """

    path = solution_root / "catalog/runtime/contracts/scientific-deployment-pools.json"
    contract = _load_json(path)
    _validate_schema(
        contract,
        _load_json(
            solution_root
            / "catalog/runtime/schema/scientific-deployment-pools.schema.json"
        ),
        "scientific deployment pools",
    )
    return contract


def _validate_deployment_placement(
    declaration: Mapping[str, Any], solution_root: Path
) -> None:
    placement = declaration["placement"]
    resources = declaration["resources"]
    contract = _deployment_pool_contract(solution_root)
    pools = {item["pool_id"]: item for item in contract["pools"]}
    requested_class = placement["accelerator_class"]
    requested_resource = placement["accelerator_resource_name"]
    for pool_id in placement["compatible_pool_ids"]:
        pool = pools.get(pool_id)
        if pool is None:
            raise OnboardingError(
                f"placement pool {pool_id} is absent from the reviewed deployment inventory"
            )
        if pool["accelerator_class"] != requested_class:
            raise OnboardingError(
                f"placement pool {pool_id} does not satisfy accelerator class {requested_class}"
            )
        if pool["accelerator_resource_name"] != requested_resource:
            raise OnboardingError(
                f"placement pool {pool_id} does not expose {requested_resource}"
            )
        if pool["gpus_per_node"] < resources["gpu_count"]:
            raise OnboardingError(
                f"placement pool {pool_id} cannot supply {resources['gpu_count']} GPUs"
            )


def _custom_validate(declaration: Mapping[str, Any]) -> None:
    model = declaration["model"]
    source = model["source"]
    runtime = declaration["runtime"]
    resources = declaration["resources"]
    serving = declaration["serving"]
    batch = declaration["batch"]

    image_repository, image_digest, image_state = _runtime_image(runtime)
    if _has_http(declaration):
        if not isinstance(runtime["image"], str) or not _valid_image_reference(
            runtime["image"]
        ):
            raise OnboardingError(
                "HTTP runtime.image must be a canonical lowercase OCI repository pinned by sha256 digest"
            )
        if image_state != "digest-pinned" or image_digest is None:
            raise OnboardingError("HTTP runtime images must be digest-pinned")
    elif not image_repository:
        raise OnboardingError("scientific batch runtime requires a logical repository")
    review = source["review"]
    repository = source["repository"]
    if (
        _HF_REPO_ID.fullmatch(repository) is None
        or "--" in repository
        or ".." in repository
        or any(
            part.startswith(("-", ".")) or part.endswith(("-", "."))
            for part in repository.split("/")
        )
    ):
        raise OnboardingError(
            "model.source.repository must be a canonical namespace/repository ID"
        )
    if source["revision"] != review["revision"]:
        raise OnboardingError(
            "model.source.revision must equal model.source.review.revision"
        )
    source_root = (
        "https://huggingface.co"
        if source["kind"] == "huggingface"
        else "https://github.com"
    )
    expected_review_url = f"{source_root}/{repository}/tree/{source['revision']}"
    if review["url"] != expected_review_url:
        raise OnboardingError(
            "model.source.review.url must equal the canonical exact-revision source tree URL"
        )
    if _has_http(declaration):
        if source["kind"] != "huggingface":
            raise OnboardingError(
                "the HTTP Deployment adapter currently requires a Hugging Face source"
            )
        service_name = _service_name(declaration)
        if service_name != model["id"] and not service_name.startswith(
            f"{model['id']}-"
        ):
            raise OnboardingError(
                "serving.service_name must be the model ID or an owned ID prefix"
            )
    if resources["gpu_count"] == 1 and resources["gpu_topology"] != "single-gpu":
        raise OnboardingError("one GPU requires gpu_topology=single-gpu")
    if (
        resources["gpu_count"] > 1
        and resources["gpu_topology"] != "single-node-multi-gpu"
    ):
        raise OnboardingError(
            "multiple GPUs require gpu_topology=single-node-multi-gpu"
        )
    accelerator_class = declaration["placement"].get(
        "accelerator_class",
        declaration["placement"]["required_node_labels"].get(
            "accelerator.fs2.nebius/class"
        ),
    )
    if not accelerator_class or (
        declaration["placement"]["required_node_labels"].get(
            "accelerator.fs2.nebius/class"
        )
        != accelerator_class
    ):
        raise OnboardingError(
            "placement.required_node_labels accelerator class must equal placement.accelerator_class"
        )
    for key, value in declaration["placement"]["required_node_labels"].items():
        if not _valid_label_key(key):
            raise OnboardingError(f"placement label key is not canonical: {key}")
        if _LABEL_VALUE.fullmatch(value) is None:
            raise OnboardingError(f"placement label value is not canonical: {value}")
    _resource_projection(resources)

    argv = [*runtime["command"], *runtime["args"]]
    model_path_references = sum(item.count(MODEL_PATH_TOKEN) for item in argv)
    if _has_http(declaration) and model_path_references != 1:
        raise OnboardingError(
            f"HTTP runtime command and args must contain exactly one {MODEL_PATH_TOKEN} token"
        )
    if _has_scientific_batch(declaration) and model_path_references > 1:
        raise OnboardingError(
            f"scientific runtime argv may reference {MODEL_PATH_TOKEN} at most once; adapters own artifact localization"
        )
    if runtime["kind"] == "vllm":
        if not _has_http(declaration):
            raise OnboardingError("vLLM requires execution_mode=http or hybrid")
        assert serving is not None
        if (
            source["repository"] in argv
            or "--revision" in argv
            or any(item.startswith("--revision=") for item in argv)
        ):
            raise OnboardingError(
                "vLLM argv must consume {MODEL_PATH}; source identity belongs to localization"
            )
        alias = _strict_argv_option(argv, "--served-model-name", allow_inline=False)
        if alias != model["id"]:
            raise OnboardingError("vLLM --served-model-name must equal model.id")
        if _strict_argv_option(argv, "--host") != "0.0.0.0":
            raise OnboardingError(
                "vLLM --host must equal 0.0.0.0 for the Deployment adapter"
            )
        if _strict_argv_option(argv, "--port") != str(serving["port"]):
            raise OnboardingError("vLLM --port must equal serving.port")
        if _strict_argv_option(argv, "--tensor-parallel-size") != str(
            resources["gpu_count"]
        ):
            raise OnboardingError(
                "vLLM --tensor-parallel-size must equal resources.gpu_count"
            )

    if _has_http(declaration):
        assert serving is not None
        _canonical_manifest_path(_manifest_path(declaration), "profile.manifest_path")
        for index, keeper_path in enumerate(
            declaration.get("profile", {}).get("keeper_paths", [])
        ):
            _canonical_manifest_path(keeper_path, f"profile.keeper_paths[{index}]")

        for protocol, path in serving["protocols"].items():
            expected = _CANONICAL_PROTOCOL_PATHS.get(protocol)
            if expected is not None and path != expected:
                raise OnboardingError(
                    f"{protocol} must use its canonical path {expected}"
                )
        if serving["operations"] != sorted(serving["operations"]):
            raise OnboardingError(
                "serving.operations must be sorted for deterministic review"
            )
        _validate_public_description(serving["mcp"]["description"], "serving")

    if _has_scientific_batch(declaration):
        assert batch is not None
        if "variant_id" not in model or "default_variant" not in model:
            raise OnboardingError(
                "scientific declarations require explicit variant_id and default_variant"
            )
        if (
            declaration["placement"].get("accelerator_resource_name")
            != "nvidia.com/gpu"
        ):
            raise OnboardingError(
                "scientific placement requires accelerator_resource_name=nvidia.com/gpu"
            )
        parameter_schema_id, _, _ = _parameter_contract(batch)
        expected_parameter_schema = _SCIENTIFIC_ADAPTER_PARAMETER_SCHEMA_IDS.get(
            model["id"],
            f"fs2-serve.nebius.ai/{model['id']}-"
            f"{model.get('variant_id', 'default')}-parameters/v1",
        )
        if not isinstance(batch["parameter_schema"], Mapping):
            raise OnboardingError(
                "scientific batch.parameter_schema must embed a fail-closed schema and digest input"
            )
        if parameter_schema_id != expected_parameter_schema:
            raise OnboardingError(
                "batch.parameter_schema.id must bind model.id and model.variant_id exactly"
            )
        if batch["operations"] != sorted(batch["operations"]):
            raise OnboardingError(
                "batch.operations must be sorted for deterministic review"
            )
        _validate_public_description(batch["mcp"]["description"], "batch")
        stage_ids: set[str] = set()
        for stage in batch["stages"]:
            stage_id = stage["id"]
            if stage_id in stage_ids:
                raise OnboardingError(f"duplicate scientific stage ID: {stage_id}")
            unknown = sorted(set(stage["needs"]) - stage_ids)
            if unknown:
                raise OnboardingError(
                    f"scientific stage {stage_id} needs unknown or later stages: "
                    + ", ".join(unknown)
                )
            if stage["min_parallelism"] > stage["max_parallelism"]:
                raise OnboardingError(
                    f"scientific stage {stage_id} min_parallelism exceeds max_parallelism"
                )
            if (
                stage["admission_mode"] == "gang-jobset"
                and stage["min_parallelism"] < 2
            ):
                raise OnboardingError(
                    f"scientific stage {stage_id} gang-jobset requires min_parallelism >= 2"
                )
            stage_resources = stage.get("resources")
            if stage_resources is not None:
                _resource_projection(stage_resources)
                stage_gpu_count = stage_resources["gpu_count"]
                if stage["resource_class"] == "cpu" and stage_gpu_count != 0:
                    raise OnboardingError(
                        f"scientific CPU stage {stage_id} cannot reserve a GPU"
                    )
                if stage["resource_class"] == "gpu" and stage_gpu_count < 1:
                    raise OnboardingError(
                        f"scientific GPU stage {stage_id} requires a GPU"
                    )
            if stage["resource_class"] == "cpu" and "placement" in stage:
                raise OnboardingError(
                    f"scientific CPU stage {stage_id} cannot declare accelerator placement"
                )
            stage_ids.add(stage_id)
        for storage in batch.get("storage", []):
            unknown_stages = sorted(set(storage["stages"]) - stage_ids)
            if unknown_stages:
                raise OnboardingError(
                    f"scientific storage {storage['id']} references unknown stages: "
                    + ", ".join(unknown_stages)
                )

    for artifact in model.get("artifacts", []):
        artifact_source = artifact["source"]
        source_kind = artifact_source["kind"]
        repository_value = artifact_source["repository"]
        revision_value = artifact_source["revision"]
        release_id = artifact_source["release_id"]
        if source_kind in {"huggingface", "source-tree"}:
            if not isinstance(repository_value, str) or not isinstance(
                revision_value, str
            ):
                raise OnboardingError(
                    f"artifact {artifact['artifact_id']} requires an exact repository revision"
                )
            if release_id is not None:
                raise OnboardingError(
                    f"artifact {artifact['artifact_id']} exact repository source cannot use release_id"
                )
        elif source_kind == "operator-staged":
            if (
                repository_value is not None
                or revision_value is not None
                or not isinstance(release_id, str)
            ):
                raise OnboardingError(
                    f"artifact {artifact['artifact_id']} operator-staged source must use only release_id"
                )

    if (
        _has_http(declaration)
        and _has_scientific_batch(declaration)
        and serving["mcp"]["tool_name"] == batch["mcp"]["tool_name"]
    ):
        raise OnboardingError(
            "hybrid serving and scientific-batch interfaces require distinct MCP tool names"
        )

    environment = runtime.get("environment", {})
    reserved = sorted(set(environment).intersection(_RESERVED_ENV))
    if reserved:
        raise OnboardingError(
            "runtime.environment cannot override compiler-owned variables: "
            + ", ".join(reserved)
        )
    sensitive = sorted(key for key in environment if _SENSITIVE_ENV.search(key))
    if sensitive:
        raise OnboardingError(
            "model declarations cannot contain credential-like environment keys: "
            + ", ".join(sensitive)
        )

    entitlement = source.get("entitlement")
    if entitlement is not None:
        if entitlement["required"] != (entitlement["state"] != "not-required"):
            raise OnboardingError(
                "entitlement.required must be false only for state=not-required"
            )
        if entitlement["required"]:
            if _has_http(declaration):
                raise OnboardingError(
                    "the HTTP Deployment adapter does not materialize gated-source credentials"
                )
            assert batch is not None
            if batch["access_profile"] != "academic":
                raise OnboardingError(
                    "a gated scientific workload requires access_profile=academic"
                )
            if batch["access_state"] != entitlement["state"]:
                raise OnboardingError(
                    "batch.access_state must equal the gated source entitlement state"
                )
        elif _has_scientific_batch(declaration):
            assert batch is not None
            if batch["access_state"] != "not-required":
                raise OnboardingError(
                    "an ungated scientific workload requires access_state=not-required"
                )
        if entitlement["credential_contract"] is not None:
            if not entitlement["required"]:
                raise OnboardingError(
                    "a not-required entitlement cannot declare a credential contract"
                )


def _validate_public_description(description: str, label: str) -> None:
    lowered = description.lower()
    if any(
        marker in lowered
        for marker in ("http://", "https://", "token", "secret", "credential")
    ):
        raise OnboardingError(
            f"{label}.mcp.description contains private routing material"
        )


def _validate_declaration_value(declaration: Mapping[str, Any]) -> None:
    schema = _load_json(DECLARATION_SCHEMA)
    _validate_schema(declaration, schema, "model declaration")
    _custom_validate(declaration)


def load_declaration(path: Path) -> dict[str, Any]:
    """Load and validate one declaration without performing I/O beyond local files."""

    declaration = _load_json(path.resolve())
    _validate_declaration_value(declaration)
    return declaration


def _catalog_record(declaration: Mapping[str, Any]) -> dict[str, Any]:
    model = declaration["model"]
    source = model["source"]
    runtime = declaration["runtime"]
    resources = declaration["resources"]
    serving = declaration["serving"]
    assert serving is not None
    policy = declaration["policy"]
    derived_resources = _resource_projection(resources)
    image_match = _IMAGE.fullmatch(runtime["image"])
    assert image_match is not None
    image_digest = image_match.group(1)
    catalog_argv = [
        CATALOG_MODEL_PATH_TOKEN if item == MODEL_PATH_TOKEN else item
        for item in [*runtime["command"], *runtime["args"]]
    ]
    limitations = sorted(
        set(
            [
                "Generated onboarding projection; retained platform qualification is required before route exposure.",
                *policy["limitations"],
            ]
        )
    )
    if declaration["execution_mode"] == "hybrid":
        limitations.append(
            "The scientific-batch interface is a separate candidate profile "
            "and is not exposed by the HTTP serving binding."
        )
    return {
        "schema": "fs2-serve.nebius.ai/model/v1",
        "model": {
            "id": model["id"],
            "display_name": model["display_name"],
            "family": model["family"],
            "tested_lane": True,
            "source": {
                "kind": source["kind"],
                "repository": source["repository"],
                "revision": source["revision"],
                "license": source["license"],
                "entitlement": source["entitlement"],
            },
        },
        "runtime": {
            "kind": runtime["kind"],
            "version": runtime["version"],
            "image": {
                "reference": runtime["image"],
                "digest": image_digest,
                "state": "resolved",
            },
            "command": catalog_argv,
        },
        "resources": {
            "cpu_millis": derived_resources["cpu_millis"],
            "memory_bytes": derived_resources["memory_bytes"],
            "host_ram_min_bytes": resources.get("host_ram_min_bytes"),
            "scaler_owner": "nebius-managed-node-group-autoscaler",
            "gpu": {
                "class": "NVIDIA-B300-SXM6-288GB",
                "count": resources["gpu_count"],
                "topology": resources["gpu_topology"],
                "placement": None,
                "b300_state": "unverified",
                "alternatives": [],
            },
        },
        "interface": {
            "execution_mode": "http",
            "protocols": sorted(serving["protocols"]),
            "endpoints": dict(sorted(serving["protocols"].items())),
            "readiness": {
                "method": "GET",
                "path": serving["readiness_path"],
                "expected_status": 200,
                "timeout_seconds": serving["startup_timeout_seconds"],
            },
            "warmup": None,
            "policy": {
                "operations": serving["operations"],
                "license_enforced": True,
                "non_clinical": policy["non_clinical"],
                "commercial_use": policy["commercial_use"],
            },
            "mcp": {"discoverable": True, "invocable": False},
        },
        "startup": {
            "default": "conventional",
            "fallback": "conventional",
            "enabled_mechanisms": ["conventional"],
            "experiments": [],
            "multi_gpu_criu": "unproven-disabled",
        },
        "cache": {
            "owner": "fs2-serve-localizer",
            "shared_path": f"/mnt/fs2-serve-cache/models/{model['id']}",
            "local_path": f"/var/lib/fs2-serve/cache/models/{model['id']}",
            "pre_pull_image": True,
            "artifact": {
                "state": "unresolved",
                "kind": "weights",
                "manifest_digest": None,
                "expanded_bytes": None,
                "minimum_bytes": None,
                "capacity_bound_bytes": None,
                "staged": False,
            },
        },
        "semantic_validator": {
            "kind": "blocked",
            "contract": "qualification-pending/v1",
            "source_path": None,
            "source_sha256": None,
            "fixture_path": None,
            "fixture_sha256": None,
            "request_count": 2,
            "distinct_requests": True,
            "distinct_responses": True,
        },
        "support": {
            "state": "unqualified",
            "route_exposed": False,
            "non_clinical": policy["non_clinical"],
            "limitations": limitations,
        },
        "evidence": [
            {
                "classification": "unverified",
                "hardware": None,
                "outcome": "unqualified",
                "source_commit": source["review"]["revision"],
                "summary": source["review"]["summary"],
            }
        ],
        "provenance": [
            {
                "url": source["review"]["url"],
                "revision": source["review"]["revision"],
                "classification": "reviewed-input",
            }
        ],
    }


def _profile_projection(declaration: Mapping[str, Any]) -> dict[str, Any]:
    model_id = declaration["model"]["id"]
    resources = declaration["resources"]
    placement = declaration["placement"]
    profile = declaration.get("profile", {})
    derived_resources = _resource_projection(resources)
    service_name = _service_name(declaration)
    manifest_path = _manifest_path(declaration)
    model_artifact: dict[str, Any] = {
        "manifest_paths": [manifest_path],
        "keeper_paths": sorted(profile.get("keeper_paths", [])),
    }
    required_secrets = sorted(profile.get("required_secrets", []))
    if required_secrets:
        model_artifact["required_secrets"] = required_secrets
    memberships = profile.get("memberships", ["full_catalog"])
    return {
        "schema": "fs2-serve.nebius.ai/model-profile-projection/v1",
        "merge_target": "catalog/profiles/model-profiles.json",
        "model_id": model_id,
        "model_artifact": model_artifact,
        "model_autoscaling_target": {
            "deployment": service_name,
            "gpu_count": resources["gpu_count"],
            "ephemeral_storage_request_gib": derived_resources[
                "ephemeral_storage_request_gib"
            ],
        },
        "workload_placement": {
            "key": service_name,
            "value": {
                "model_id": model_id,
                "runtime_variant": f"deployment/{service_name}",
                "state": "fixture-only",
                "gpu_request": resources["gpu_count"],
                "workload_topology": resources["gpu_topology"],
                "host_architectures": sorted(placement["host_architectures"]),
                "selection_mode": "accelerator-class",
                "compatible_pool_ids": list(placement["compatible_pool_ids"]),
                "required_node_labels": dict(
                    sorted(placement["required_node_labels"].items())
                ),
            },
        },
        "profile_memberships": {
            membership: {
                "canonical_routes": [model_id],
                "manifest_paths": [manifest_path],
                "keeper_paths": sorted(profile.get("keeper_paths", [])),
            }
            for membership in sorted(memberships)
        },
    }


def _route_projection(declaration: Mapping[str, Any]) -> dict[str, Any]:
    model = declaration["model"]
    runtime = declaration["runtime"]
    serving = declaration["serving"]
    image_match = _IMAGE.fullmatch(runtime["image"])
    assert image_match is not None
    return {
        "schema": "fs2-serve.nebius.ai/live-service-route-projection/v1",
        "merge_target": "components/control-plane/contracts/all-models-live-services.json",
        "model_id": model["id"],
        "route": {
            "variant_id": None,
            "model_revision": model["source"]["revision"],
            "runtime_image_digest": image_match.group(1),
            "service": {
                "name": _service_name(declaration),
                "port": serving["port"],
            },
            "storage_mode": serving["storage_mode"],
            "protocols": dict(sorted(serving["protocols"].items())),
            "operations": serving["operations"],
            "mcp": {
                "enabled": True,
                "tool_name": serving["mcp"]["tool_name"],
                "description": serving["mcp"]["description"],
            },
        },
        "qualification_lists": {
            "action": "none",
            "reason": "Live qualification evidence, not a declaration, owns these lists.",
        },
    }


def _scientific_workload_projection(
    declaration: Mapping[str, Any], solution_root: Path
) -> dict[str, Any]:
    """Render a candidate batch contract; this does not create a Job or route."""

    model = declaration["model"]
    source = model["source"]
    runtime = declaration["runtime"]
    resources = declaration["resources"]
    placement = declaration["placement"]
    batch = declaration["batch"]
    policy = declaration["policy"]
    assert batch is not None
    image_repository, image_digest, image_state = _runtime_image(runtime)
    parameter_schema_id, parameter_schema_document, parameter_schema_sha256 = (
        _parameter_contract(batch)
    )
    poc_authorized = batch.get("poc_authorization") == "user-authorized-academic-poc"
    runtime_recipe = {
        "template": runtime["template"],
        "kind": runtime["kind"],
        "version": runtime["version"],
        "image": runtime["image"],
        "command": runtime["command"],
        "args": runtime["args"],
        "working_directory": runtime.get("working_directory"),
        "environment": dict(sorted(runtime.get("environment", {}).items())),
    }
    workload_recipe = {
        "parameter_schema": parameter_schema_id,
        "parameter_schema_sha256": parameter_schema_sha256,
        "operations": batch["operations"],
        "stages": batch["stages"],
        "storage": batch.get("storage", []),
    }
    limitations = sorted(
        set(
            [
                "Candidate contract only: no BatchRun controller, Kueue Job, "
                "route, or credential is generated.",
                "A source revision observation is not workload, hardware, "
                "semantic, or license qualification.",
                *policy["limitations"],
            ]
        )
    )
    adapter_runtime_recipe_sha256 = _scientific_adapter_runtime_recipe_sha256(
        solution_root, model["id"]
    )
    profile = {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": model["id"],
        "variant_id": model.get("variant_id", "default"),
        "default_variant": model.get("default_variant", False),
        "display_name": model["display_name"],
        "execution_mode": declaration["execution_mode"],
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": source["kind"],
            "repository": source["repository"],
            "revision": source["revision"],
            "review_url": source["review"]["url"],
            "classification": "candidate-input",
        },
        "execution_identity": {
            "model_revision": source["revision"],
            "runtime_image_repository": image_repository,
            "runtime_image_state": image_state,
            "runtime_image_digest": image_digest,
            "runtime_recipe_sha256": adapter_runtime_recipe_sha256
            or hashlib.sha256(_canonical_bytes(runtime_recipe)).hexdigest(),
            "workload_recipe_sha256": hashlib.sha256(
                _canonical_bytes(workload_recipe)
            ).hexdigest(),
            "artifact_manifest_digest": None,
            "execution_identity_sha256": None,
        },
        "interface": {
            "protocol": batch["protocol"],
            "variant_submit_endpoint": (
                f"/v1/models/{model['id']}/variants/"
                f"{model.get('variant_id', 'default')}:submit"
            ),
            "default_submit_endpoint": (
                f"/v1/models/{model['id']}:submit"
                if model.get("default_variant", False)
                else None
            ),
            "request_schema": batch["request_schema"],
            "result_schema": batch["result_schema"],
            "parameter_schema": parameter_schema_id,
            "parameter_schema_sha256": parameter_schema_sha256,
            "parameter_schema_definition": parameter_schema_document,
            "operations": batch["operations"],
            "service_classes": batch["service_classes"],
            "mcp": {
                "discoverable": True,
                "invocable": False,
                "tool_name": batch["mcp"]["tool_name"],
                "description": batch["mcp"]["description"],
            },
        },
        "access": {
            "profile": batch["access_profile"],
            "state": batch["access_state"],
            "receipt_digest": None,
            "credentials_embedded": False,
            "condition": (
                "LicenseAcceptancePending"
                if batch["access_state"] == "license-acceptance-pending"
                else None
            ),
            "materialization": (
                "restricted-quarantine-poc-authorized"
                if poc_authorized
                else "quarantine-required"
                if batch["access_state"] == "license-acceptance-pending"
                else "not-materialized"
            ),
            "operational_activation": (
                "user-authorized-academic-poc"
                if poc_authorized
                else "poc-quarantine-only"
                if batch["access_state"] == "license-acceptance-pending"
                else "candidate-not-activated"
            ),
            "formal_license_acceptance": (
                "pending-authorized-institution-representative"
                if batch["access_state"] == "license-acceptance-pending"
                else "not-required"
            ),
            "license_gate_scope": (
                "production-promotion-only"
                if poc_authorized
                else "materialization-and-execution"
                if batch["access_state"] == "license-acceptance-pending"
                else "not-applicable"
            ),
        },
        "artifact_requirements": [dict(item) for item in model.get("artifacts", [])],
        "resources": {
            "gpu_count": resources["gpu_count"],
            "gpu_topology": resources["gpu_topology"],
            "host_architectures": sorted(placement["host_architectures"]),
            "compatible_pool_ids": list(placement["compatible_pool_ids"]),
            "required_node_labels": dict(
                sorted(placement["required_node_labels"].items())
            ),
            "accelerator_requirement": {
                "class": placement["accelerator_class"],
                "resource_name": placement["accelerator_resource_name"],
                "count": resources["gpu_count"],
            },
            "cache_pvc": {
                "size_bytes": _binary_bytes(resources["cache_pvc"]["size"]),
                "storage_class": resources["cache_pvc"]["storage_class"],
                "binding": "deployment-configured-claim",
            },
        },
        "workload": {
            "parameter_schema_sha256": parameter_schema_sha256,
            "stages": [
                {
                    **{
                        key: value for key, value in stage.items() if key != "resources"
                    },
                    **(
                        {
                            "resources": {
                                **_resource_projection(stage["resources"]),
                                "gpu_count": stage["resources"]["gpu_count"],
                                "limits": {
                                    "cpu_millis": _cpu_millis(
                                        stage["resources"]["limits"]["cpu"]
                                    ),
                                    "memory_bytes": _binary_bytes(
                                        stage["resources"]["limits"]["memory"]
                                    ),
                                    "ephemeral_storage_bytes": _binary_bytes(
                                        stage["resources"]["limits"][
                                            "ephemeral_storage"
                                        ]
                                    ),
                                },
                            }
                        }
                        if "resources" in stage
                        else {}
                    ),
                    "placement": (
                        {
                            "accelerator_requirement": {
                                "class": placement["accelerator_class"],
                                "resource_name": placement["accelerator_resource_name"],
                                "count": stage.get("resources", {}).get(
                                    "gpu_count", resources["gpu_count"]
                                ),
                            },
                            "compatible_pool_ids": list(
                                placement["compatible_pool_ids"]
                            ),
                            "required_node_labels": dict(
                                sorted(placement["required_node_labels"].items())
                            ),
                        }
                        if stage["resource_class"] == "gpu"
                        else {
                            "accelerator_requirement": None,
                            "compatible_pool_ids": [],
                            "required_node_labels": {},
                        }
                    ),
                }
                for stage in batch["stages"]
            ],
            "storage": batch.get("storage", []),
            "retry": batch["retry"],
            "cancellation": batch["cancellation"],
        },
        "semantic_validation": batch["semantic_validation"],
        "policy": {
            "commercial_use": policy["commercial_use"],
            "non_clinical": policy["non_clinical"],
            "limitations": limitations,
            "fast_start": policy.get(
                "fast_start",
                {
                    "requested_level": "Off",
                    "maximum_level": "L1",
                    "qualified_level": "Off",
                    "state": "candidate-unqualified",
                    "cache_strategy": "shared-pvc",
                },
            ),
        },
    }
    profile["execution_identity"]["workload_recipe_sha256"] = hashlib.sha256(
        _canonical_bytes(profile["workload"])
    ).hexdigest()
    return {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
        "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json",
        "profile": profile,
    }


def compile_scientific_profile(
    declaration: Mapping[str, Any], solution_root: Path
) -> dict[str, Any]:
    """Compile and schema-check one canonical scientific profile without merging it."""

    _validate_declaration_value(declaration)
    if _has_scientific_batch(declaration):
        _validate_deployment_placement(declaration, solution_root)
    if not _has_scientific_batch(declaration):
        raise OnboardingError("declaration has no scientific-batch interface")
    projection = _scientific_workload_projection(declaration, solution_root)
    profile_schema_path = (
        solution_root / "catalog/runtime/schema/scientific-workload-profile.schema.json"
    )
    if not profile_schema_path.is_file():
        raise OnboardingError(f"solution root does not contain {profile_schema_path}")
    _validate_schema(
        projection["profile"],
        _load_json(profile_schema_path),
        "scientific workload profile projection",
    )
    return projection["profile"]


def _catalog_index_projection(declaration: Mapping[str, Any]) -> dict[str, Any]:
    model_id = declaration["model"]["id"]
    return {
        "schema": "fs2-serve.nebius.ai/catalog-index-projection/v1",
        "merge_target": "catalog/runtime/catalog.json",
        "preconditions": {
            "model_file_absent": f"{model_id}.json",
            "model_id_absent": model_id,
        },
        "sorted_set_additions": {
            "model_files": [f"{model_id}.json"],
            "tested_model_ids": [model_id],
        },
        "blocked_candidate_ids": {"action": "none"},
    }


def _validate_collisions(declaration: Mapping[str, Any], solution_root: Path) -> None:
    """Reject a new-model declaration that would replace an owned solution entry."""

    model_id = declaration["model"]["id"]
    collisions: set[str] = set()

    model_path = solution_root / f"catalog/runtime/models/{model_id}.json"
    if _has_http(declaration) and (model_path.exists() or model_path.is_symlink()):
        collisions.add(f"catalog model {model_id}")
    http_tool_name: str | None = None
    batch_tool_name: str | None = None
    if _has_http(declaration):
        service_name = _service_name(declaration)
        assert declaration["serving"] is not None
        http_tool_name = declaration["serving"]["mcp"]["tool_name"]
        manifest_path = _manifest_path(declaration)
        manifest = solution_root / manifest_path
        if manifest.exists() or manifest.is_symlink():
            collisions.add(f"manifest {manifest_path}")
    if _has_scientific_batch(declaration):
        assert declaration["batch"] is not None
        batch_tool_name = declaration["batch"]["mcp"]["tool_name"]
        if declaration["model"].get("default_variant", False) and model_path.is_file():
            collisions.add(
                f"default scientific submit route /v1/models/{model_id}:submit conflicts with canonical catalog model"
            )

    catalog_path = solution_root / "catalog/runtime/catalog.json"
    if catalog_path.is_file():
        catalog = _load_json(catalog_path)
        if _has_http(declaration) and f"{model_id}.json" in catalog.get(
            "model_files", []
        ):
            collisions.add(f"catalog index file {model_id}.json")
        if _has_http(declaration) and model_id in catalog.get("tested_model_ids", []):
            collisions.add(f"catalog index model {model_id}")

    profile_path = solution_root / "catalog/profiles/model-profiles.json"
    if _has_http(declaration) and profile_path.is_file():
        profiles = _load_json(profile_path)
        if model_id in profiles.get("model_artifacts", {}):
            collisions.add(f"model profile {model_id}")
        if model_id in profiles.get("model_autoscaling_targets", {}):
            collisions.add(f"autoscaling target {model_id}")
        placement = profiles.get("workload_placements", {}).get(service_name)
        if placement is not None:
            collisions.add(
                f"workload placement {service_name} (model={placement.get('model_id', 'unknown')})"
            )
        for profile_name, profile in profiles.get("profiles", {}).items():
            if model_id in profile.get("canonical_routes", []):
                collisions.add(f"profile route {profile_name}/{model_id}")
            if manifest_path in profile.get("manifest_paths", []):
                collisions.add(f"profile manifest {profile_name}/{manifest_path}")

    inventory_path = (
        solution_root
        / "components/control-plane/contracts/all-models-live-services.json"
    )
    if inventory_path.is_file():
        inventory = _load_json(inventory_path)
        for existing_model_id, route in inventory.get("routes", {}).items():
            existing_tool_name = route.get("mcp", {}).get("tool_name")
            if _has_http(declaration):
                if existing_model_id == model_id:
                    collisions.add(f"live-service route {model_id}")
                if route.get("service", {}).get("name") == service_name:
                    collisions.add(
                        f"live service {service_name} (model={existing_model_id})"
                    )
                if http_tool_name is not None and existing_tool_name == http_tool_name:
                    collisions.add(
                        f"MCP tool {http_tool_name} (model={existing_model_id})"
                    )
            if batch_tool_name is not None and existing_tool_name == batch_tool_name:
                collisions.add(
                    f"scientific MCP tool {batch_tool_name} conflicts with live-service model={existing_model_id}"
                )

    scientific_profiles_path = (
        solution_root / "catalog/runtime/contracts/scientific-workload-profiles.json"
    )
    if scientific_profiles_path.is_file():
        profiles = _load_json(scientific_profiles_path)
        variant_id = declaration["model"].get("variant_id", "default")
        variant_endpoint = f"/v1/models/{model_id}/variants/{variant_id}:submit"
        default_endpoint = (
            f"/v1/models/{model_id}:submit"
            if declaration["model"].get("default_variant", False)
            else None
        )
        image_repository, _, _ = _runtime_image(declaration["runtime"])
        for profile in profiles.get("profiles", []):
            same_identity = (
                profile.get("model_id") == model_id
                and profile.get("variant_id", "default") == variant_id
            )
            same_checked_in_declaration = (
                same_identity
                and profile.get("source", {}).get("revision")
                == declaration["model"]["source"]["revision"]
                and profile.get("interface", {}).get("mcp", {}).get("tool_name")
                == batch_tool_name
            )
            if same_checked_in_declaration:
                continue
            if same_identity:
                collisions.add(f"scientific workload profile {model_id}/{variant_id}")
            existing_variant_endpoint = profile.get("interface", {}).get(
                "variant_submit_endpoint"
            )
            existing_default_endpoint = profile.get("interface", {}).get(
                "default_submit_endpoint"
            )
            if existing_variant_endpoint == variant_endpoint:
                collisions.add(f"scientific variant route {variant_endpoint}")
            if (
                default_endpoint is not None
                and existing_default_endpoint == default_endpoint
            ):
                collisions.add(f"scientific default route {default_endpoint}")
            if (
                profile.get("execution_identity", {}).get("runtime_image_repository")
                == image_repository
            ):
                collisions.add(
                    f"scientific runtime repository {image_repository} "
                    f"(model={profile.get('model_id', 'unknown')})"
                )
            existing_tool = profile.get("interface", {}).get("mcp", {}).get("tool_name")
            if batch_tool_name is not None and existing_tool == batch_tool_name:
                collisions.add(
                    f"scientific MCP tool {batch_tool_name} "
                    f"(model={profile.get('model_id', 'unknown')})"
                )
            if http_tool_name is not None and existing_tool == http_tool_name:
                collisions.add(
                    f"MCP tool {http_tool_name} conflicts with scientific model="
                    f"{profile.get('model_id', 'unknown')}"
                )

    if collisions:
        raise OnboardingError(
            "declaration collides with existing solution entries: "
            + "; ".join(sorted(collisions))
        )


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _yaml_map_lines(values: Mapping[str, str], *, indent: int) -> list[str]:
    prefix = " " * indent
    return [
        f"{prefix}{_yaml_string(key)}: {_yaml_string(value)}"
        for key, value in sorted(values.items())
    ]


def _manifest(declaration: Mapping[str, Any]) -> bytes:
    model = declaration["model"]
    source = model["source"]
    runtime = declaration["runtime"]
    resources = declaration["resources"]
    placement = declaration["placement"]
    serving = declaration["serving"]
    model_id = model["id"]
    service_name = _service_name(declaration)
    image_match = _IMAGE.fullmatch(runtime["image"])
    assert image_match is not None
    image_digest = image_match.group(1)
    runtime_command = [
        "/model-cache/content" if item == MODEL_PATH_TOKEN else item
        for item in runtime["command"]
    ]
    runtime_args = [
        "/model-cache/content" if item == MODEL_PATH_TOKEN else item
        for item in runtime["args"]
    ]
    failure_threshold = max(1, (serving["startup_timeout_seconds"] + 9) // 10)
    environment = {
        "FS2_MODEL_ID": model_id,
        "FS2_MODEL_REPOSITORY": source["repository"],
        "FS2_MODEL_REVISION": source["revision"],
        "HF_HOME": "/model-cache/.cache/huggingface",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HOME": "/runtime-cache/home",
        **runtime.get("environment", {}),
    }
    labels = {
        "app.kubernetes.io/component": "model-runtime",
        "app.kubernetes.io/instance": service_name,
        "app.kubernetes.io/managed-by": "fs2-serve-model-onboarding",
        "app.kubernetes.io/name": model_id,
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/model-id": model_id,
    }
    label_lines = _yaml_map_lines(labels, indent=4)
    pod_label_lines = _yaml_map_lines(labels, indent=8)
    selector_lines = _yaml_map_lines(placement["required_node_labels"], indent=8)
    env_lines: list[str] = []
    for name, value in sorted(environment.items()):
        env_lines.extend(
            [
                f"            - name: {name}",
                f"              value: {_yaml_string(value)}",
            ]
        )
    source_env = [
        ("FS2_MODEL_REPOSITORY", source["repository"]),
        ("FS2_MODEL_REVISION", source["revision"]),
    ]
    localizer_env_lines: list[str] = []
    for name, value in source_env:
        localizer_env_lines.extend(
            [
                f"            - name: {name}",
                f"              value: {_yaml_string(value)}",
            ]
        )
    lines = [
        "apiVersion: v1",
        "kind: ServiceAccount",
        "metadata:",
        f"  name: {service_name}",
        "  namespace: fs2-models",
        "  labels:",
        *_yaml_map_lines(labels, indent=4),
        "automountServiceAccountToken: false",
        "---",
        "apiVersion: v1",
        "kind: PersistentVolumeClaim",
        "metadata:",
        f"  name: {service_name}-cache",
        "  namespace: fs2-models",
        "  labels:",
        *_yaml_map_lines(labels, indent=4),
        "spec:",
        "  accessModes: [ReadWriteOnce]",
        "  resources:",
        "    requests:",
        f"      storage: {resources['cache_pvc']['size']}",
        f"  storageClassName: {resources['cache_pvc']['storage_class']}",
        "---",
        "apiVersion: apps/v1",
        "kind: Deployment",
        "metadata:",
        f"  name: {service_name}",
        "  namespace: fs2-models",
        "  labels:",
        *label_lines,
        "  annotations:",
        f"    fs2.nebius/model-repository: {_yaml_string(source['repository'])}",
        f"    fs2.nebius/model-revision: {_yaml_string(source['revision'])}",
        f"    fs2.nebius/runtime-image-digest: {_yaml_string(image_digest)}",
        "spec:",
        "  replicas: 0",
        "  revisionHistoryLimit: 2",
        "  strategy: {type: Recreate}",
        "  selector:",
        "    matchLabels:",
        f"      app.kubernetes.io/instance: {service_name}",
        "  template:",
        "    metadata:",
        "      labels:",
        *pod_label_lines,
        "      annotations:",
        f"        fs2.nebius/model-revision: {_yaml_string(source['revision'])}",
        f"        fs2.nebius/runtime-image-digest: {_yaml_string(image_digest)}",
        "    spec:",
        f"      serviceAccountName: {service_name}",
        "      automountServiceAccountToken: false",
        "      enableServiceLinks: false",
        "      terminationGracePeriodSeconds: 120",
        "      securityContext:",
        "        runAsNonRoot: true",
        "        runAsUser: 1000",
        "        runAsGroup: 1000",
        "        fsGroup: 1000",
        "        fsGroupChangePolicy: OnRootMismatch",
        "        seccompProfile: {type: RuntimeDefault}",
        "      nodeSelector:",
        *selector_lines,
        "      tolerations:",
        "        - {key: dedicated, operator: Equal, value: fs2-inference, effect: NoSchedule}",
        "      initContainers:",
        "        - name: localize-model",
        f"          image: {_yaml_string(runtime['image'])}",
        "          imagePullPolicy: IfNotPresent",
        "          command: [python3, -c]",
        "          args:",
        "            - |",
        "              import os",
        "              from huggingface_hub import snapshot_download",
        "              snapshot_download(",
        '                  repo_id=os.environ["FS2_MODEL_REPOSITORY"],',
        '                  revision=os.environ["FS2_MODEL_REVISION"],',
        '                  local_dir="/model-cache/content",',
        "              )",
        "          env:",
        *localizer_env_lines,
        "          resources:",
        '            requests: {cpu: "2", memory: 4Gi, ephemeral-storage: 1Gi}',
        '            limits: {cpu: "8", memory: 16Gi, ephemeral-storage: 8Gi}',
        "          securityContext:",
        "            allowPrivilegeEscalation: false",
        "            capabilities: {drop: [ALL]}",
        "          volumeMounts:",
        "            - {name: model-cache, mountPath: /model-cache}",
        "      containers:",
        "        - name: runtime",
        f"          image: {_yaml_string(runtime['image'])}",
        "          imagePullPolicy: IfNotPresent",
        f"          command: {json.dumps(runtime_command, separators=(',', ':'))}",
    ]
    if runtime_args:
        lines.append(
            f"          args: {json.dumps(runtime_args, separators=(',', ':'))}"
        )
    lines.extend(
        [
            "          env:",
            *env_lines,
            "          ports:",
            f"            - {{name: http, containerPort: {serving['port']}, protocol: TCP}}",
            "          resources:",
            "            requests:",
            f"              cpu: {_yaml_string(resources['requests']['cpu'])}",
            f"              memory: {_yaml_string(resources['requests']['memory'])}",
            f"              ephemeral-storage: {_yaml_string(resources['requests']['ephemeral_storage'])}",
            f"              nvidia.com/gpu: {_yaml_string(str(resources['gpu_count']))}",
            "            limits:",
            f"              cpu: {_yaml_string(resources['limits']['cpu'])}",
            f"              memory: {_yaml_string(resources['limits']['memory'])}",
            f"              ephemeral-storage: {_yaml_string(resources['limits']['ephemeral_storage'])}",
            f"              nvidia.com/gpu: {_yaml_string(str(resources['gpu_count']))}",
            "          startupProbe:",
            f"            httpGet: {{path: {_yaml_string(serving['readiness_path'])}, port: http}}",
            "            periodSeconds: 10",
            f"            failureThreshold: {failure_threshold}",
            "          readinessProbe:",
            f"            httpGet: {{path: {_yaml_string(serving['readiness_path'])}, port: http}}",
            "            periodSeconds: 5",
            "            failureThreshold: 3",
            "          livenessProbe:",
            f"            httpGet: {{path: {_yaml_string(serving['liveness_path'])}, port: http}}",
            "            periodSeconds: 15",
            "            failureThreshold: 4",
            "          securityContext:",
            "            allowPrivilegeEscalation: false",
            "            capabilities: {drop: [ALL]}",
            "          volumeMounts:",
            "            - {name: model-cache, mountPath: /model-cache, readOnly: true}",
            "            - {name: runtime-cache, mountPath: /runtime-cache}",
            "            - {name: tmp, mountPath: /tmp}",
            "      volumes:",
            f"        - name: model-cache\n          persistentVolumeClaim: {{claimName: {service_name}-cache}}",
            "        - name: runtime-cache\n          emptyDir: {sizeLimit: 16Gi}",
            "        - name: tmp\n          emptyDir: {sizeLimit: 8Gi}",
            "---",
            "apiVersion: v1",
            "kind: Service",
            "metadata:",
            f"  name: {service_name}",
            "  namespace: fs2-models",
            "  labels:",
            *_yaml_map_lines(labels, indent=4),
            "spec:",
            "  selector:",
            f"    app.kubernetes.io/instance: {service_name}",
            "  ports:",
            f"    - {{name: http, port: {serving['port']}, targetPort: http, protocol: TCP}}",
            "---",
            "apiVersion: networking.k8s.io/v1",
            "kind: NetworkPolicy",
            "metadata:",
            f"  name: {service_name}",
            "  namespace: fs2-models",
            "  labels:",
            *_yaml_map_lines(labels, indent=4),
            "spec:",
            "  podSelector:",
            "    matchLabels:",
            f"      app.kubernetes.io/instance: {service_name}",
            "  policyTypes: [Ingress, Egress]",
            "  ingress:",
            "    - from:",
            "        - namespaceSelector:",
            "            matchLabels:",
            "              kubernetes.io/metadata.name: fs2-system",
            "      ports:",
            f"        - {{port: {serving['port']}, protocol: TCP}}",
            "  egress:",
            "    - to:",
            "        - namespaceSelector:",
            "            matchLabels:",
            "              kubernetes.io/metadata.name: kube-system",
            "      ports:",
            "        - {port: 53, protocol: UDP}",
            "        - {port: 53, protocol: TCP}",
            "    - to:",
            "        - ipBlock: {cidr: 0.0.0.0/0}",
            "      ports:",
            "        - {port: 443, protocol: TCP}",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def compile_artifacts(
    declaration: Mapping[str, Any], solution_root: Path
) -> tuple[Artifact, ...]:
    """Build all staged bytes and validate projections against current schemas."""

    _validate_declaration_value(declaration)
    if _has_scientific_batch(declaration):
        _validate_deployment_placement(declaration, solution_root)
    _validate_collisions(declaration, solution_root)
    model_id = declaration["model"]["id"]
    base: list[Artifact] = []
    if _has_http(declaration):
        model_record = _catalog_record(declaration)
        model_schema_path = solution_root / "catalog/runtime/schema/model.schema.json"
        if not model_schema_path.is_file():
            raise OnboardingError(f"solution root does not contain {model_schema_path}")
        _validate_schema(
            model_record, _load_json(model_schema_path), "catalog model projection"
        )
        manifest_path = _manifest_path(declaration)
        base.extend(
            [
                Artifact(
                    path=f"catalog/runtime/models/{model_id}.json",
                    target=f"catalog/runtime/models/{model_id}.json",
                    payload=_json_bytes(model_record),
                    kind="catalog-model",
                ),
                Artifact(
                    path=manifest_path,
                    target=manifest_path,
                    payload=_manifest(declaration),
                    kind="kubernetes-manifest",
                ),
                Artifact(
                    path="projections/model-profile.json",
                    target="catalog/profiles/model-profiles.json",
                    payload=_json_bytes(_profile_projection(declaration)),
                    kind="merge-projection",
                ),
                Artifact(
                    path="projections/live-service-route.json",
                    target="components/control-plane/contracts/all-models-live-services.json",
                    payload=_json_bytes(_route_projection(declaration)),
                    kind="merge-projection",
                ),
                Artifact(
                    path="projections/catalog-index.json",
                    target="catalog/runtime/catalog.json",
                    payload=_json_bytes(_catalog_index_projection(declaration)),
                    kind="merge-projection",
                ),
            ]
        )
    if _has_scientific_batch(declaration):
        profile = compile_scientific_profile(declaration, solution_root)
        projection = {
            "schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
            "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json",
            "profile": profile,
        }
        base.append(
            Artifact(
                path="projections/scientific-workload-profile.json",
                target=projection["merge_target"],
                payload=_json_bytes(projection),
                kind="merge-projection",
            )
        )
    declaration_digest = hashlib.sha256(_canonical_bytes(declaration)).hexdigest()
    bundle = {
        "schema": COMPILER_SCHEMA,
        "compiler_version": 2,
        "model_id": model_id,
        "variant_id": declaration["model"].get("variant_id"),
        "declaration_sha256": declaration_digest,
        "promotion_ready": False,
        "artifacts": [
            {
                "kind": artifact.kind,
                "path": artifact.path,
                "target": artifact.target,
                "bytes": len(artifact.payload),
                "sha256": artifact.sha256,
            }
            for artifact in sorted(base, key=lambda item: item.path)
        ],
        "remaining_promotion_work": list(_PROMOTION_GAPS),
    }
    return tuple(
        sorted(
            [
                *base,
                Artifact(
                    path="onboarding-bundle.json",
                    target="review-only",
                    payload=_json_bytes(bundle),
                    kind="bundle-index",
                ),
            ],
            key=lambda item: item.path,
        )
    )


def _files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    if root.is_symlink() or not root.is_dir():
        raise OnboardingError(f"output root is not a regular directory: {root}")
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }


def _safe_target(root: Path, relative: str) -> Path:
    target = root / relative
    try:
        target.resolve(strict=False).relative_to(root.resolve())
    except ValueError as error:
        raise OnboardingError(
            f"generated path escapes output root: {relative}"
        ) from error
    current = target.parent
    while current != root.parent and current != root:
        if current.exists() and current.is_symlink():
            raise OnboardingError(f"generated parent is a symlink: {current}")
        current = current.parent
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise OnboardingError(f"generated target is not a regular file: {target}")
    return target


def _output_root(root: Path) -> Path:
    absolute = root.expanduser().absolute()
    if absolute.is_symlink():
        raise OnboardingError(f"output root cannot be a symlink: {absolute}")
    return absolute.resolve()


def write_artifacts(root: Path, artifacts: Iterable[Artifact]) -> None:
    """Atomically update the exact generated set without deleting unknown files."""

    root = _output_root(root)
    artifact_list = tuple(artifacts)
    expected = {artifact.path for artifact in artifact_list}
    unexpected = sorted(_files(root) - expected)
    if unexpected:
        raise OnboardingError(
            "output directory contains files outside this bundle: "
            + ", ".join(unexpected)
        )
    root.mkdir(parents=True, exist_ok=True)
    for artifact in artifact_list:
        target = _safe_target(root, artifact.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            stream.write(artifact.payload)
            temporary = Path(stream.name)
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def check_artifacts(root: Path, artifacts: Iterable[Artifact]) -> tuple[str, ...]:
    """Return deterministic drift messages without changing the output tree."""

    root = _output_root(root)
    artifact_list = tuple(artifacts)
    expected = {artifact.path for artifact in artifact_list}
    actual = _files(root)
    messages = [f"missing: {path}" for path in sorted(expected - actual)]
    messages.extend(f"unexpected: {path}" for path in sorted(actual - expected))
    by_path = {artifact.path: artifact for artifact in artifact_list}
    for relative in sorted(expected.intersection(actual)):
        target = _safe_target(root, relative)
        if target.read_bytes() != by_path[relative].payload:
            messages.append(f"different: {relative}")
    return tuple(messages)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "generate", "check", "dry-run"):
        child = subparsers.add_parser(command)
        child.add_argument("declaration", type=Path)
        child.add_argument(
            "--solution-root",
            type=Path,
            default=DEFAULT_SOLUTION_ROOT,
            help="k8s-inference solution root used only to validate current schemas",
        )
        if command in {"generate", "check"}:
            child.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        declaration = load_declaration(args.declaration)
        artifacts = compile_artifacts(declaration, args.solution_root.resolve())
        if args.command == "validate":
            print(
                f"model declaration: PASS model={declaration['model']['id']} "
                f"artifacts={len(artifacts)}"
            )
            return 0
        if args.command == "dry-run":
            print(
                json.dumps(
                    {
                        "model_id": declaration["model"]["id"],
                        "writes": False,
                        "artifacts": [
                            {
                                "path": artifact.path,
                                "target": artifact.target,
                                "bytes": len(artifact.payload),
                                "sha256": artifact.sha256,
                            }
                            for artifact in artifacts
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "generate":
            write_artifacts(args.output_dir, artifacts)
            print(
                f"model onboarding generate: PASS model={declaration['model']['id']} "
                f"output={args.output_dir.resolve()}"
            )
            return 0
        drift = check_artifacts(args.output_dir, artifacts)
        if drift:
            for message in drift:
                print(message, file=sys.stderr)
            print("model onboarding check: FAIL", file=sys.stderr)
            return 1
        print(
            f"model onboarding check: PASS model={declaration['model']['id']} "
            f"output={args.output_dir.resolve()}"
        )
        return 0
    except (OSError, OnboardingError) as error:
        print(f"model onboarding: FAIL ({error})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
