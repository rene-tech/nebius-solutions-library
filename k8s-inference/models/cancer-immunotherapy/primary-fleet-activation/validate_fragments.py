#!/usr/bin/env python3
"""Validate and render model-owned primary scientific activation inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PROFILE_SCHEMA = ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json"
REQUEST_SCHEMA = ROOT / "catalog/runtime/schema/scientific-run-request.schema.json"
RESULT_SCHEMA = ROOT / "catalog/runtime/schema/scientific-run-result.schema.json"
ARTIFACT_MANIFEST_SCHEMA = (
    ROOT / "catalog/runtime/schema/scientific-artifact-manifest.schema.json"
)
FRAGMENTS = {
    "proteina-complexa": ROOT
    / "models/cancer-immunotherapy/runtime-images/proteina-complexa/activation/fragment.json",
    "mosaic": ROOT
    / "models/cancer-immunotherapy/runtime-images/mosaic/activation/fragment.json",
    "bindcraft": ROOT
    / "models/cancer-immunotherapy/images/bindcraft-native/activation/fragment.json",
    "rfdiffusion": ROOT
    / "models/cancer-immunotherapy/runtime-images/rfdiffusion/activation/fragment.json",
}
FORBIDDEN_AGGREGATES = {
    "catalog/runtime/contracts/scientific-workload-profiles.json",
    "catalog/runtime/contracts/scientific-execution-map.json",
}
PROVIDER_ID = re.compile(r"(?:computeinstance|mk8scluster|project|tenant)-e00[0-9a-z]+")
REPOSITORY_PREFIX = "k8s-inference/"
# Values that are computed from the shared recipe inputs rather than authored.
# A model lane may not add, remove or edit anything else in a shared aggregate.
DERIVED_IDENTITY_FIELDS = frozenset(
    {
        "runtime_recipe_sha256",
        "workload_recipe_sha256",
        "execution_identity_sha256",
        "execution_map_sha256",
    }
)
# This one aggregate cleanup is owned by the serialized production repair, not
# by any model activation lane.  The old execution map declared the entire
# reference-data plane in addition to two exact BoltzGen generation mounts.
# The renderer correctly rejects that unbound broad declaration.  Permit only
# its all-stage removal while continuing to reject every other authored leaf.
BOLTZGEN_GPU_STAGES = frozenset(
    {"configure", "design", "inverse-folding", "folding", "design-folding", "affinity"}
)
BOLTZGEN_LEGACY_BROAD_MOUNT = {
    "name": "reference-data",
    "kind": "reference",
    "claim_name": None,
    "host_path": "/mnt/fs2-reference-data/data",
    "mount_path": "/reference-data",
    "sub_path": None,
    "read_only": True,
}
INTEGRATION_SOURCE_REVISION = "003064c440c4ab198bf96957e435a7aac8da6800"
SHARED_RUNTIME_RECIPE_PATHS = frozenset(
    {
        "components/control-plane/src/fs2_serve/scientific_batch/__init__.py",
        "components/control-plane/src/fs2_serve/scientific_batch/controller.py",
        "components/control-plane/src/fs2_serve/scientific_batch/models.py",
        "components/control-plane/src/fs2_serve/scientific_batch/catalog_adapter.py",
        "components/control-plane/src/fs2_serve/scientific_batch/protocols.py",
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/__init__.py",
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/common.py",
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/primitives.py",
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/materialization.py",
        "components/control-plane/src/fs2_serve/scientific_batch/adapters/localization.py",
        "catalog/runtime/schema/scientific-run-request.schema.json",
        "catalog/runtime/schema/scientific-run-result.schema.json",
        "catalog/runtime/schema/scientific-artifact-localization.schema.json",
        "catalog/runtime/contracts/scientific-artifact-localization.json",
    }
)
MODEL_RUNTIME_RECIPE_PATHS = {
    "proteina-complexa": frozenset(
        {
            "components/control-plane/src/fs2_serve/scientific_batch/adapters/proteina_complexa.py",
            "catalog/runtime/schema/proteina-complexa-parameters.schema.json",
            "models/structure/batch-adapters/proteina-complexa/adapter.py",
            "models/structure/batch-adapters/proteina-complexa/contract.json",
        }
    ),
    "bindcraft": frozenset(
        {
            "components/control-plane/src/fs2_serve/scientific_batch/adapters/bindcraft.py",
            "models/structure/batch-adapters/bindcraft/contract.json",
            "models/cancer-immunotherapy/images/bindcraft-native/image-lock.json",
            "models/cancer-immunotherapy/images/bindcraft-native/runtime/runtime_entrypoint.py",
        }
    ),
    "mosaic": frozenset(
        {
            "models/cancer-immunotherapy/runtime-images/mosaic/image-lock.json",
            "models/cancer-immunotherapy/runtime-images/mosaic/runtime_entrypoint.py",
            "models/cancer-immunotherapy/runtime-images/mosaic/qualification/render_plan.py",
            "models/cancer-immunotherapy/runtime-images/mosaic/qualification/validate_result.py",
        }
    ),
    "rfdiffusion": frozenset(
        {
            "models/cancer-immunotherapy/runtime-images/rfdiffusion/image-lock.json",
            "models/cancer-immunotherapy/runtime-images/rfdiffusion/runtime_entrypoint.py",
            "models/cancer-immunotherapy/runtime-images/rfdiffusion/qualification/render_job.py",
            "models/cancer-immunotherapy/runtime-images/rfdiffusion/qualification/validate_result.py",
        }
    ),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def repository_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    path.relative_to(ROOT)
    return path


def validate_schema(instance: Any, schema_path: Path, label: str) -> list[str]:
    validator = Draft202012Validator(
        load_json(schema_path), format_checker=FormatChecker()
    )
    return [
        f"{label}{'.' + '.'.join(str(item) for item in error.path) if error.path else ''}: {error.message}"
        for error in sorted(
            validator.iter_errors(instance), key=lambda error: list(error.path)
        )
    ]


def git_is_ancestor(revision: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", revision, "HEAD"],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def runtime_recipe_sha256(paths: list[str]) -> str:
    """Hash exact source paths with the control-plane recipe algorithm."""

    digest = hashlib.sha256()
    for relative in sorted(paths):
        content = repository_path(relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _identity_is_present(document: Any, value: str) -> bool:
    return value in json.dumps(document, sort_keys=True, separators=(",", ":"))


def validate_fragment(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Validate the fragment on disk at ``path``."""

    fragment = load_json(path)
    return fragment, validate_fragment_document(fragment, path)


def validate_fragment_document(fragment: dict[str, Any], path: Path) -> list[str]:
    """Validate an in-memory fragment, so a test can probe a hypothetical one.

    ``path`` is still needed because a fragment names its own model by directory
    when it omits ``model_id``; nothing is read from it.
    """

    model_id = fragment.get("model_id", path.parent.parent.name)
    errors = validate_schema(fragment, HERE / "fragment.schema.json", model_id)
    if errors:
        return errors

    accepted = fragment["accepted_evidence"]
    profile = fragment["profile_projection"]["profile"]
    execution = fragment["execution_projection"]
    image = accepted["runtime_image"]
    recipe = accepted["runtime_recipe"]
    source = accepted["source"]

    errors.extend(validate_schema(profile, PROFILE_SCHEMA, f"{model_id}.profile"))
    if profile["model_id"] != model_id:
        errors.append(f"{model_id}: profile model_id differs")
    if profile["state"] != "candidate-unqualified" or profile["route_exposed"]:
        errors.append(f"{model_id}: profile must remain an unrouted candidate")
    if profile["source"]["classification"] != "candidate-input":
        errors.append(f"{model_id}: profile source must remain candidate-input")
    if profile["source"]["revision"] != source["revision"]:
        errors.append(f"{model_id}: accepted and profile source revisions differ")
    identity = profile["execution_identity"]
    if identity["runtime_image_digest"] != image["digest"]:
        errors.append(f"{model_id}: accepted and profile image digests differ")
    expected_recipe_paths = (
        SHARED_RUNTIME_RECIPE_PATHS | MODEL_RUNTIME_RECIPE_PATHS[model_id]
    )
    if recipe["source_revision"] != INTEGRATION_SOURCE_REVISION:
        errors.append(f"{model_id}: runtime recipe source is not the integration head")
    elif not git_is_ancestor(recipe["source_revision"]):
        errors.append(f"{model_id}: runtime recipe source is not in current HEAD")
    if set(recipe["paths"]) != expected_recipe_paths:
        errors.append(f"{model_id}: runtime recipe source path set differs")
    else:
        try:
            expected_runtime_recipe = runtime_recipe_sha256(recipe["paths"])
        except FileNotFoundError as error:
            errors.append(
                f"{model_id}: runtime recipe source is missing: {error.filename}"
            )
        else:
            if identity["runtime_recipe_sha256"] != expected_runtime_recipe:
                errors.append(f"{model_id}: runtime recipe digest is stale")
    expected_workload_recipe = hashlib.sha256(
        json.dumps(profile["workload"], sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    if identity["workload_recipe_sha256"] != expected_workload_recipe:
        errors.append(f"{model_id}: workload recipe digest is stale")
    if (
        identity["artifact_manifest_digest"] is not None
        or identity["execution_identity_sha256"] is not None
    ):
        errors.append(f"{model_id}: candidate identity must remain incomplete")
    if profile["interface"]["mcp"]["invocable"]:
        errors.append(f"{model_id}: candidate MCP tool must not be invocable")
    if profile["semantic_validation"]["state"] != "candidate-unqualified":
        errors.append(
            f"{model_id}: public semantic state must remain candidate-unqualified"
        )

    lock_path = repository_path(image["lock_path"])
    digest_evidence_path = repository_path(image["digest_evidence_path"])
    if not lock_path.is_file():
        errors.append(f"{model_id}: image lock does not exist: {image['lock_path']}")
    if not digest_evidence_path.is_file():
        errors.append(
            f"{model_id}: image digest evidence does not exist: {image['digest_evidence_path']}"
        )
    if lock_path.is_file() and digest_evidence_path.is_file():
        identity_documents = [load_json(lock_path), load_json(digest_evidence_path)]
        for label, value in (
            ("source revision", source["revision"]),
            ("image digest", image["digest"]),
        ):
            if not any(
                _identity_is_present(document, value) for document in identity_documents
            ):
                errors.append(
                    f"{model_id}: {label} is absent from its lock and digest evidence"
                )
    if image["reference"].split("@", 1)[1] != image["digest"]:
        errors.append(f"{model_id}: image reference and digest differ")

    for evidence_path in accepted["h100"]["evidence_paths"]:
        if not repository_path(evidence_path).is_file():
            errors.append(f"{model_id}: H100 evidence is missing: {evidence_path}")
    for revision in accepted["main_commits"]:
        if not git_is_ancestor(revision):
            errors.append(
                f"{model_id}: accepted commit is not in current HEAD: {revision}"
            )

    artifacts = {item["artifact_id"]: item for item in execution["runtime_artifacts"]}
    if len(artifacts) != len(execution["runtime_artifacts"]):
        errors.append(f"{model_id}: duplicate runtime artifact ids")
    for artifact in artifacts.values():
        source_binding = artifact["source"]
        generation = artifact["generation"]
        if artifact["state"] == "ready":
            if (
                generation is None
                or artifact["content_digest"] != f"sha256:{generation}"
            ):
                errors.append(
                    f"{model_id}/{artifact['artifact_id']}: ready generation identity is incomplete"
                )
            suffix = f"generations/{artifact['artifact_id']}/sha256/{generation}"
            if not source_binding["sub_path"] or not source_binding[
                "sub_path"
            ].endswith(suffix):
                errors.append(
                    f"{model_id}/{artifact['artifact_id']}: generation subPath is not exact"
                )
            if source_binding["kind"] == "unresolved" or source_binding["root"] is None:
                errors.append(
                    f"{model_id}/{artifact['artifact_id']}: ready artifact has unresolved source"
                )
        else:
            if any(
                (
                    generation,
                    artifact["content_digest"],
                    source_binding["root"],
                    source_binding["sub_path"],
                )
            ):
                errors.append(
                    f"{model_id}/{artifact['artifact_id']}: blocked artifact invents a generation binding"
                )
            if source_binding["kind"] != "unresolved":
                errors.append(
                    f"{model_id}/{artifact['artifact_id']}: blocked artifact must be unresolved"
                )
        if not repository_path(artifact["evidence_path"]).is_file():
            errors.append(
                f"{model_id}/{artifact['artifact_id']}: evidence path is missing"
            )

    artifact_evidence_paths = {
        repository_path(artifact["evidence_path"]) for artifact in artifacts.values()
    }
    artifact_evidence = [
        load_json(evidence_path)
        for evidence_path in artifact_evidence_paths
        if evidence_path.is_file()
    ]
    for content_identity in execution["artifact_identity_inputs"]:
        if not any(
            _identity_is_present(document, content_identity)
            for document in artifact_evidence
        ):
            errors.append(
                f"{model_id}: artifact identity lacks current-main evidence: {content_identity}"
            )

    all_ready = all(item["state"] == "ready" for item in artifacts.values())
    if execution["state"] == "ready-for-serialized-integration":
        if not all_ready or execution["blockers"]:
            errors.append(
                f"{model_id}: ready execution has blocked artifacts or blockers"
            )
    elif all_ready or not execution["blockers"]:
        errors.append(
            f"{model_id}: blocked execution must name an unresolved artifact and blocker"
        )

    profile_stages = {item["id"]: item for item in profile["workload"]["stages"]}
    execution_stages = {item["id"]: item for item in execution["stages"]}
    if profile_stages.keys() != execution_stages.keys():
        errors.append(f"{model_id}: profile and execution stage ids differ")
    for stage_id, stage in execution_stages.items():
        unknown = set(stage["runtime_artifacts"]) - artifacts.keys()
        if unknown:
            errors.append(
                f"{model_id}/{stage_id}: unknown runtime artifacts: {sorted(unknown)}"
            )
        placement = stage["placement"]
        if stage["resource_class"] == "gpu":
            if placement["accelerator_classes"] != ["nvidia-h100-sxm5-80gb"]:
                errors.append(
                    f"{model_id}/{stage_id}: GPU accelerator capability is not exact"
                )
            if set(placement["eligible_pool_ids"]) != {"h100-1x", "h100-reserved-8x"}:
                errors.append(
                    f"{model_id}/{stage_id}: eligible H100 pools are incomplete"
                )
        else:
            if placement["accelerator_classes"] or placement["eligible_pool_ids"]:
                errors.append(f"{model_id}/{stage_id}: CPU stage carries GPU placement")
        if stage["image"] != image["reference"]:
            errors.append(
                f"{model_id}/{stage_id}: stage does not use accepted immutable image"
            )

    fixture_paths = fragment["public_fixtures"]
    request_path = repository_path(fixture_paths["request"])
    result_path = repository_path(fixture_paths["result"])
    validator_path = repository_path(fixture_paths["semantic_validator"])
    for fixture_path in (request_path, result_path, validator_path):
        if not fixture_path.is_file():
            errors.append(
                f"{model_id}: referenced fixture/validator is missing: {fixture_path.relative_to(ROOT)}"
            )
    if request_path.is_file():
        request = load_json(request_path)
        errors.extend(validate_schema(request, REQUEST_SCHEMA, f"{model_id}.request"))
        if request.get("operation") not in profile["interface"]["operations"]:
            errors.append(f"{model_id}: request operation is absent from profile")
        supporting_artifacts: set[tuple[str, int]] = set()
        input_manifests: list[dict[str, Any]] = []
        for supporting in fixture_paths.get("supporting_inputs", []):
            supporting_path = repository_path(supporting["path"])
            if not supporting_path.is_file():
                errors.append(
                    f"{model_id}: supporting request input is missing: {supporting['path']}"
                )
                continue
            content = supporting_path.read_bytes()
            if supporting["encoding"] == "canonical-json-newline":
                supporting_value = load_json(supporting_path)
                errors.extend(
                    validate_schema(
                        supporting_value,
                        ARTIFACT_MANIFEST_SCHEMA,
                        f"{model_id}.input_manifest",
                    )
                )
                content = (
                    json.dumps(
                        supporting_value, sort_keys=True, separators=(",", ":")
                    ).encode("ascii")
                    + b"\n"
                )
                input_manifests.append(supporting_value)
            content_identity = (hashlib.sha256(content).hexdigest(), len(content))
            if supporting["role"] == "request-input-manifest":
                pointer = request["input_manifest"]
                if (pointer["sha256"], pointer["size_bytes"]) != content_identity:
                    errors.append(
                        f"{model_id}: public request pointer does not match its supporting input"
                    )
            else:
                supporting_artifacts.add(content_identity)
        for input_manifest in input_manifests:
            for entry in input_manifest["entries"]:
                pointer = entry["artifact"]
                if (
                    pointer["sha256"],
                    pointer["size_bytes"],
                ) not in supporting_artifacts:
                    errors.append(
                        f"{model_id}: input manifest artifact lacks exact supporting bytes: {pointer['artifact_id']}"
                    )
    if result_path.is_file():
        result = load_json(result_path)
        errors.extend(validate_schema(result, RESULT_SCHEMA, f"{model_id}.result"))
        if (
            result.get("terminal_status") != "failed"
            or result.get("semantic_validation", {}).get("status") != "not-run"
        ):
            errors.append(
                f"{model_id}: pre-activation result fixture must fail before semantic execution"
            )
        if result.get("error", {}).get("code") != "ACTIVATION_NOT_ENABLED":
            errors.append(f"{model_id}: result fixture must name the activation gate")
        result_identity = result.get("execution_identity", {})
        for label, expected in (
            ("model_id", model_id),
            ("variant_id", execution["variant_id"]),
            ("model_revision", source["revision"]),
            ("runtime_image_digest", image["digest"]),
        ):
            if result_identity.get(label) != expected:
                errors.append(f"{model_id}: result {label} does not match the fragment")
        for label in ("runtime_recipe_sha256", "workload_recipe_sha256"):
            if result_identity.get(label) != identity[label]:
                errors.append(f"{model_id}: result {label} does not match the profile")
        artifact_identity = hashlib.sha256(
            json.dumps(
                sorted(execution["artifact_identity_inputs"]), separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        if result_identity.get("model_artifact_manifest_digest") != artifact_identity:
            errors.append(f"{model_id}: result artifact identity is not deterministic")
        executable_identity = {
            "model_id": model_id,
            "variant_id": execution["variant_id"],
            "model_revision": source["revision"],
            "runtime_image_digest": image["digest"],
            "runtime_recipe_sha256": identity["runtime_recipe_sha256"],
            "workload_recipe_sha256": identity["workload_recipe_sha256"],
            "model_artifact_manifest_digest": artifact_identity,
        }
        expected_execution_identity = hashlib.sha256(
            json.dumps(
                executable_identity, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        if (
            result_identity.get("execution_identity_sha256")
            != expected_execution_identity
        ):
            errors.append(f"{model_id}: result execution identity is not deterministic")

    serialized = json.dumps(fragment, sort_keys=True, separators=(",", ":"))
    if PROVIDER_ID.search(serialized):
        errors.append(f"{model_id}: provider-specific resource id found")
    return errors


def _json_leaves(
    value: Any, path: tuple[Any, ...] = ()
) -> Iterator[tuple[tuple[Any, ...], Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _json_leaves(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _json_leaves(item, path + (index,))
    else:
        yield path, value


def _aggregate_at_baseline(relative: str) -> Any:
    completed = subprocess.run(
        ["git", "show", f"{INTEGRATION_SOURCE_REVISION}:{REPOSITORY_PREFIX}{relative}"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return json.loads(completed.stdout)


def _normalize_serialized_boltzgen_mount_cleanup(
    relative: str, baseline: dict[str, Any], current: dict[str, Any]
) -> None:
    """Normalize only the reviewed all-GPU-stage removal of one inert broad mount."""

    if relative != "catalog/runtime/contracts/scientific-execution-map.json":
        return
    baseline_models = {
        item.get("model_id"): item
        for item in baseline.get("models", [])
        if isinstance(item, dict)
    }
    current_models = {
        item.get("model_id"): item
        for item in current.get("models", [])
        if isinstance(item, dict)
    }
    baseline_model = baseline_models.get("boltzgen")
    current_model = current_models.get("boltzgen")
    if not isinstance(baseline_model, dict) or not isinstance(current_model, dict):
        return
    baseline_stages = {
        item.get("stage_id"): item
        for item in baseline_model.get("stages", [])
        if isinstance(item, dict)
    }
    current_stages = {
        item.get("stage_id"): item
        for item in current_model.get("stages", [])
        if isinstance(item, dict)
    }
    if not all(
        isinstance(baseline_stages.get(stage_id), dict)
        and isinstance(current_stages.get(stage_id), dict)
        and BOLTZGEN_LEGACY_BROAD_MOUNT in baseline_stages[stage_id].get("mounts", [])
        and BOLTZGEN_LEGACY_BROAD_MOUNT
        not in current_stages[stage_id].get("mounts", [])
        for stage_id in BOLTZGEN_GPU_STAGES
    ):
        return
    for stage_id in BOLTZGEN_GPU_STAGES:
        baseline_stages[stage_id]["mounts"].remove(BOLTZGEN_LEGACY_BROAD_MOUNT)


def _derived_identity_refresh_only(relative: str) -> list[str]:
    """Classify a change to a shared aggregate as derived-only, or report why not.

    The guard exists so a model lane cannot activate itself by writing its own
    profile or execution-map entry. It is not meant to forbid the derived
    identity digests, which are a pure function of the shared recipe inputs: any
    change to those inputs, such as integrating a new localization transform,
    makes every pinned digest stale, and the repository's own
    ``scripts/refresh_scientific_recipes.py`` is what recomputes them. So a
    change is accepted only when no leaf is added or removed and every differing
    leaf is one of the derived digests. Whether the new digests are *correct* is
    proven separately, by
    ``components/control-plane/tests/test_scientific_primary_adapters.py``.
    """
    try:
        baseline_document = _aggregate_at_baseline(relative)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return [f"forbidden shared aggregate changed: {relative}"]
    current_document = load_json(repository_path(relative))
    if not isinstance(baseline_document, dict) or not isinstance(
        current_document, dict
    ):
        return [f"forbidden shared aggregate changed: {relative}"]
    _normalize_serialized_boltzgen_mount_cleanup(
        relative, baseline_document, current_document
    )
    baseline = dict(_json_leaves(baseline_document))
    current = dict(_json_leaves(current_document))
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    changed = sorted(
        key for key in set(baseline) & set(current) if baseline[key] != current[key]
    )
    errors = []
    for key in added:
        errors.append(
            f"shared aggregate {relative} adds authored content: {_leaf_name(key)}"
        )
    for key in removed:
        errors.append(f"shared aggregate {relative} removes content: {_leaf_name(key)}")
    for key in changed:
        if key[-1] not in DERIVED_IDENTITY_FIELDS:
            errors.append(
                f"shared aggregate {relative} changes authored content: {_leaf_name(key)}"
            )
    return errors


def _leaf_name(key: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in key)


def validate_no_aggregate_edits() -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--relative",
            "--name-only",
            INTEGRATION_SOURCE_REVISION,
            "--",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    changed = set(completed.stdout.splitlines())
    errors: list[str] = []
    for path in sorted(changed & FORBIDDEN_AGGREGATES):
        errors.extend(_derived_identity_refresh_only(path))
    return errors


def render(fragment: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic handoff material; never mutate shared aggregate files."""
    return {
        "schema": "fs2.nebius.ai/primary-scientific-activation-handoff/v1",
        "model_id": fragment["model_id"],
        "profile": fragment["profile_projection"]["profile"],
        "execution_generator_input": fragment["execution_projection"],
        "activation_gate": fragment["activation_gate"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", choices=sorted(FRAGMENTS))
    args = parser.parse_args()

    fragments: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for model_id, path in FRAGMENTS.items():
        fragment, fragment_errors = validate_fragment(path)
        fragments[model_id] = fragment
        errors.extend(fragment_errors)
    errors.extend(validate_no_aggregate_edits())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    if args.render:
        print(json.dumps(render(fragments[args.render]), indent=2, sort_keys=True))
    else:
        states = {
            model: item["execution_projection"]["state"]
            for model, item in fragments.items()
        }
        print(json.dumps({"valid": True, "models": states}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
