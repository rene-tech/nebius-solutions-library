#!/usr/bin/env python3
"""Validate the five model-owned secondary-fleet public acceptance inputs.

The check is offline.  It proves that ``run_acceptance.py`` can load and bind
every declared byte, that each request and rebuilt manifest is schema-valid,
and that the payload is the same bounded input used by the H100 qualification
renderer. ``--compile-adapters`` additionally compiles every resulting verified
manifest entry through the production registry. It never contacts a cluster or
a public endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
SOLUTION_ROOT = HERE.parents[1]
CONTROL_PLANE_SRC = SOLUTION_ROOT / "components/control-plane/src"
QUALIFICATION_ROOT = (
    SOLUTION_ROOT
    / "models/cancer-immunotherapy/images/structure-secondary/qualification"
)
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
INPUT_SCHEMA = HERE / "public-acceptance-input.schema.json"
REQUEST_SCHEMA = (
    SOLUTION_ROOT / "catalog/runtime/schema/scientific-run-request.schema.json"
)
MANIFEST_SCHEMA = (
    SOLUTION_ROOT / "catalog/runtime/schema/scientific-artifact-manifest.schema.json"
)

MODEL_DIRECTORIES = {
    "alphafold3": "alphafold3",
    "esmfold2": "esmfold2",
    "esmfold2-fast": "esmfold2-fast",
    "openfold3-openbind": "openfold3",
    "protenix-v2": "protenix-v2",
}
EXPECTED_MODELS = frozenset(MODEL_DIRECTORIES)


class ValidationError(RuntimeError):
    """One committed public acceptance input is inconsistent."""


def _load_module(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        existing_path = getattr(existing, "__file__", None)
        if existing_path is None or Path(existing_path).resolve() != path.resolve():
            raise ValidationError(
                f"module name {name!r} is already bound to another path"
            )
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_module("fs2_scientific_fleet_acceptance", HERE / "run_acceptance.py")


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValidationError(f"{path}: invalid JSON") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{path}: expected a JSON object")
    return value


def _schema_errors(value: object, schema_path: Path) -> list[str]:
    validator = Draft202012Validator(
        _object(schema_path), format_checker=FormatChecker()
    )
    return [
        f"{'.'.join(str(item) for item in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(
            validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
    ]


def _qualification_inputs() -> dict[str, bytes]:
    if str(QUALIFICATION_ROOT) not in sys.path:
        sys.path.insert(0, str(QUALIFICATION_ROOT))
    openfold = _load_module(
        "render_semantic_job", QUALIFICATION_ROOT / "render_semantic_job.py"
    )
    secondary = _load_module(
        "render_secondary_semantic_job",
        QUALIFICATION_ROOT / "render_secondary_semantic_job.py",
    )
    alphafold = _load_module(
        "render_af3_semantic_job",
        QUALIFICATION_ROOT / "render_af3_semantic_job.py",
    )
    esm = secondary.canonical(secondary.ESM_INPUT).encode("utf-8")
    return {
        "alphafold3": alphafold.FOLD_INPUT.read_bytes(),
        "esmfold2": esm,
        "esmfold2-fast": esm,
        "openfold3-openbind": openfold.canonical(openfold.RAW_INPUT).encode("utf-8"),
        "protenix-v2": secondary.PROTENIX_FIXTURE.read_bytes(),
    }


def _verified_artifact(model_id: str, entry: dict[str, Any]) -> Any:
    from fs2_serve.scientific_batch import ScientificInputArtifact

    pointer = entry["artifact"]
    return ScientificInputArtifact(
        logical_artifact_id=entry["name"],
        semantic_type=entry["semantic_type"],
        artifact_id=uuid5(NAMESPACE_URL, f"fs2-public-acceptance/{model_id}/input"),
        digest=f"sha256:{pointer['sha256']}",
        size_bytes=pointer["size_bytes"],
        media_type=pointer["media_type"],
        compression=pointer.get("compression"),
    )


def _compile_fixture(
    model_id: str,
    directory: str,
    request: dict[str, Any],
    entry: dict[str, Any],
    variant_id: str,
) -> list[str]:
    if str(CONTROL_PLANE_SRC) not in sys.path:
        sys.path.insert(0, str(CONTROL_PLANE_SRC))
    from fs2_serve.scientific_batch import compile_adapter_run
    from fs2_serve.scientific_batch.adapters.production_registry import (
        install_production_adapters,
    )

    install_production_adapters()
    projection = _object(ADAPTER_ROOT / directory / "activation/workload-profile.json")
    profile = projection.get("profile")
    if not isinstance(profile, dict):
        raise ValidationError(f"{model_id}: workload profile projection is invalid")
    plan = compile_adapter_run(
        model_id,
        profile,
        request,
        operation_id=f"acceptance-{model_id}-offline-validation",
        variant_id=variant_id,
        input_artifacts=(_verified_artifact(model_id, entry),),
    )
    raw_digest = entry["artifact"]["sha256"]
    if not any(raw_digest in invocation.argv for invocation in plan.invocations):
        raise ValidationError(f"{model_id}: compiled plan lost the raw input digest")
    return [invocation.stage_id for invocation in plan.invocations]


def validate_all(*, compile_adapters: bool = False) -> dict[str, Any]:
    qualified_inputs = _qualification_inputs()
    if set(qualified_inputs) != EXPECTED_MODELS:
        raise ValidationError("qualification input set is incomplete")
    summary: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/scientific-public-acceptance-validation/v1",
        "models": {},
    }
    for model_id in sorted(MODEL_DIRECTORIES):
        directory = MODEL_DIRECTORIES[model_id]
        activation = ADAPTER_ROOT / directory / "activation/public-acceptance.json"
        fragment = _object(activation)
        errors = _schema_errors(fragment, INPUT_SCHEMA)
        if errors:
            raise ValidationError(
                f"{model_id}: activation input schema: {'; '.join(errors)}"
            )
        if fragment["model_id"] != model_id:
            raise ValidationError(f"{model_id}: activation input names another model")

        config = RUNNER.RunConfig(
            endpoint="https://acceptance.invalid",
            repository_root=SOLUTION_ROOT,
            activation_fragment=activation,
            receipt_path=SOLUTION_ROOT / ".unused-public-acceptance-receipt.json",
            run_id=f"offline-{model_id}",
        )
        loaded_model, request, declarations, loaded_fragment = RUNNER._activation(
            config
        )
        if loaded_model != model_id or loaded_fragment != fragment:
            raise ValidationError(f"{model_id}: runner activation projection drifted")
        request_errors = _schema_errors(request, REQUEST_SCHEMA)
        if request_errors:
            raise ValidationError(
                f"{model_id}: request schema: {'; '.join(request_errors)}"
            )

        manifest_inputs = [
            item for item in declarations if item.role == "request-input-manifest"
        ]
        payload_inputs = [
            item for item in declarations if item.role == "manifest-artifact"
        ]
        if len(manifest_inputs) != 1 or len(payload_inputs) != 1:
            raise ValidationError(f"{model_id}: expected one manifest and one payload")
        manifest_input = manifest_inputs[0]
        RUNNER._verify_declared_bytes(
            RUNNER._artifact_ref(
                request.get("input_manifest"), "request_input_artifact_invalid"
            ),
            manifest_input.data,
        )
        manifest = json.loads(manifest_input.data)
        manifest_errors = _schema_errors(manifest, MANIFEST_SCHEMA)
        if manifest_errors:
            raise ValidationError(
                f"{model_id}: manifest schema: {'; '.join(manifest_errors)}"
            )
        bindings = RUNNER._entry_inputs(manifest, payload_inputs)
        if len(bindings) != 1:
            raise ValidationError(
                f"{model_id}: manifest did not bind exactly one payload"
            )
        entry, declared = bindings[0]

        semantic = fragment["semantic_input"]
        payload_path = (SOLUTION_ROOT / semantic["payload_path"]).resolve(strict=True)
        payload_path.relative_to(SOLUTION_ROOT.resolve(strict=True))
        if payload_path != declared.path:
            raise ValidationError(
                f"{model_id}: provenance and runner payload paths differ"
            )
        payload = payload_path.read_bytes()
        if payload != qualified_inputs[model_id]:
            raise ValidationError(
                f"{model_id}: payload differs from the H100 qualification input"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != semantic["sha256"] or len(payload) != semantic["size_bytes"]:
            raise ValidationError(f"{model_id}: semantic input identity is stale")

        source_path_text, source_symbol = semantic["source_contract"].rsplit("#", 1)
        source_path = (SOLUTION_ROOT / source_path_text).resolve(strict=True)
        source_path.relative_to(SOLUTION_ROOT.resolve(strict=True))
        if source_symbol not in source_path.read_text(encoding="utf-8"):
            raise ValidationError(f"{model_id}: qualification source locator is stale")
        evidence = semantic["h100_evidence"]
        if evidence is not None and not (SOLUTION_ROOT / evidence).is_file():
            raise ValidationError(f"{model_id}: declared H100 evidence is absent")

        contract = _object(ADAPTER_ROOT / directory / "contract.json")
        projection = _object(
            ADAPTER_ROOT / directory / "activation/workload-profile.json"
        )
        profile = projection.get("profile")
        if not isinstance(profile, dict):
            raise ValidationError(f"{model_id}: workload profile projection is invalid")
        interface = profile.get("interface")
        workload = profile.get("workload")
        if not isinstance(interface, dict) or not isinstance(workload, dict):
            raise ValidationError(f"{model_id}: workload profile is incomplete")
        if (
            contract.get("model_id") != model_id
            or contract.get("variant_id")
            != fragment["execution_projection"]["variant_id"]
            or profile.get("model_id") != model_id
            or request["operation"] not in interface.get("operations", [])
        ):
            raise ValidationError(f"{model_id}: request and adapter contract differ")
        stages = [item["id"] for item in workload.get("stages", [])]
        if not stages:
            raise ValidationError(f"{model_id}: workload has no stages")
        if compile_adapters:
            stages = _compile_fixture(
                model_id,
                directory,
                request,
                entry,
                fragment["execution_projection"]["variant_id"],
            )
        summary["models"][model_id] = {
            "activation_input": str(activation.relative_to(SOLUTION_ROOT)),
            "manifest_sha256": hashlib.sha256(manifest_input.data).hexdigest(),
            "payload_sha256": digest,
            "payload_size_bytes": len(payload),
            "request_sha256": hashlib.sha256(
                RUNNER._canonical_json(request, newline=True)
            ).hexdigest(),
            "stages": stages,
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact canonical JSON instead of indented JSON",
    )
    parser.add_argument(
        "--compile-adapters",
        action="store_true",
        help=(
            "also compile every fixture "
            "(requires the control-plane development dependencies)"
        ),
    )
    arguments = parser.parse_args()
    summary = validate_all(compile_adapters=arguments.compile_adapters)
    separators = (",", ":") if arguments.compact else None
    print(
        json.dumps(
            summary,
            indent=None if arguments.compact else 2,
            sort_keys=True,
            separators=separators,
        )
    )


if __name__ == "__main__":
    main()
