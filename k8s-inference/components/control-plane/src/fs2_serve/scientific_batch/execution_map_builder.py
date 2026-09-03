"""Build the installed v3 execution map from catalog and deployment evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from .adapters import stage_execution_contracts
from .compiler_cache import (
    CACHE_PREPARATION,
    CACHE_RUN_AS_GROUP,
    CACHE_RUN_AS_USER,
    CACHE_SUB_PATH_LAYOUT,
    CompilerCacheIdentity,
    accelerator_sm,
    artifact_set_sha256,
)
from .execution import FileScientificManifestRenderer, ScientificExecutionMapError
from .profile_catalog import ScientificProfileCatalog

TARGET_SCHEMA = "fs2-serve.nebius.ai/scientific-execution-targets/v1"
LOCALIZATION_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-localizations/v1"
EXECUTION_SCHEMA = "fs2-serve.nebius.ai/scientific-execution-map/v3"
WORKSPACE_MOUNT: dict[str, Any] = {
    "name": "artifact-workspace",
    "kind": "artifact-workspace",
    "artifact_id": None,
    "claim_name": None,
    "claim_namespace": None,
    "host_path": None,
    "operator_owned": False,
    "mount_path": "/mnt/fs2-scientific",
    "sub_path": None,
    "read_only": False,
    "supplemental_groups": [],
}


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ScientificExecutionMapError(f"{label} is not an object")
    return cast(Mapping[str, Any], value)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _cpu_quantity(millis: object) -> str:
    if not isinstance(millis, int) or isinstance(millis, bool) or millis < 1:
        raise ScientificExecutionMapError("profile stage CPU request is invalid")
    return str(millis // 1000) if millis % 1000 == 0 else f"{millis}m"


def _gib_quantity(value: object, label: str) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ScientificExecutionMapError(f"{label} is invalid")
    gib = 1024**3
    if value % gib:
        raise ScientificExecutionMapError(f"{label} is not an exact GiB quantity")
    return f"{value // gib}Gi"


def _validate_localization_requirement(requirement: Mapping[str, Any], localization: Mapping[str, Any]) -> None:
    artifact_id = requirement.get("artifact_id")
    content_digest = localization.get("content_digest")
    if not isinstance(content_digest, str) or content_digest.removeprefix("sha256:") != requirement.get(
        "content_digest_sha256"
    ):
        raise ScientificExecutionMapError(f"runtime artifact {artifact_id} content identity differs")
    raw_files = requirement.get("file_manifest")
    localized_files = localization.get("file_manifest")
    raw_tree = requirement.get("aggregate_tree")
    localized_tree = localization.get("aggregate_tree")
    if raw_tree is None:
        if not isinstance(raw_files, list) or not isinstance(localized_files, list) or localized_tree is not None:
            raise ScientificExecutionMapError(f"runtime artifact {artifact_id} file evidence is incomplete")
        expected = {
            (item.get("path"), item.get("sha256"), item.get("size_bytes"))
            for item in raw_files
            if isinstance(item, Mapping)
        }
        actual = {
            (item.get("path"), str(item.get("sha256", "")).removeprefix("sha256:"), item.get("size_bytes"))
            for item in localized_files
            if isinstance(item, Mapping)
        }
        if expected != actual or set(cast(list[str], requirement.get("required_files", []))) != {
            item[0] for item in actual
        }:
            raise ScientificExecutionMapError(f"runtime artifact {artifact_id} file manifest differs")
    else:
        tree = _object(localized_tree, f"runtime artifact {artifact_id} aggregate tree")
        if (
            localized_files is not None
            or not isinstance(raw_tree, Mapping)
            or requirement.get("localization_manifest_sha256")
            != str(tree.get("manifest_digest", "")).removeprefix("sha256:")
            or any(
                raw_tree.get(field) != tree.get(field)
                for field in ("dataset_relative_path", "dataset_uri", "file_count")
            )
            or requirement.get("required_files") != [".fs2-manifest-sha256"]
        ):
            raise ScientificExecutionMapError(f"runtime artifact {artifact_id} aggregate-tree evidence differs")


def _target_mount(
    raw_mount: Mapping[str, Any],
    *,
    localizations: Mapping[str, Mapping[str, Any]],
    cache_identity: CompilerCacheIdentity | None,
) -> dict[str, Any]:
    artifact_id = raw_mount.get("artifact_id")
    sub_path = raw_mount.get("sub_path")
    if raw_mount.get("kind") == "operator-host-path":
        if not isinstance(artifact_id, str) or artifact_id not in localizations:
            raise ScientificExecutionMapError("operator hostPath has no aggregate-tree localization")
        aggregate = _object(localizations[artifact_id].get("aggregate_tree"), "operator aggregate tree")
        exact_sub_path = aggregate.get("dataset_relative_path")
        if sub_path != (
            "datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/{content_digest_sha256}"
        ):
            raise ScientificExecutionMapError("operator hostPath policy does not use the canonical pinned template")
        sub_path = exact_sub_path
    elif raw_mount.get("kind") == "cache":
        if cache_identity is None or sub_path is not None:
            raise ScientificExecutionMapError("cache target has no immutable compiler-cache identity")
        sub_path = cache_identity.sub_path
    result = {
        key: value
        for key, value in {
            "name": raw_mount.get("name"),
            "kind": raw_mount.get("kind"),
            "artifact_id": artifact_id,
            "claim_name": raw_mount.get("claim_name"),
            "claim_namespace": raw_mount.get("claim_namespace"),
            "host_path": raw_mount.get("host_path"),
            "operator_owned": raw_mount.get("operator_owned"),
            "mount_path": raw_mount.get("mount_path"),
            "sub_path": sub_path,
            "read_only": raw_mount.get("read_only"),
            "supplemental_groups": raw_mount.get("supplemental_groups", []),
        }.items()
    }
    if cache_identity is not None:
        result["cache_identity"] = cache_identity.marker()
    return result


def _cache_artifact_set(
    artifact_ids: tuple[str, ...],
    localizations: Mapping[str, Mapping[str, Any]],
) -> str:
    identities: list[dict[str, object]] = []
    for artifact_id in artifact_ids:
        localization = localizations[artifact_id]
        aggregate = localization.get("aggregate_tree")
        if aggregate is not None:
            manifest_sha256 = str(_object(aggregate, "compiler-cache aggregate tree").get("manifest_digest", ""))
        else:
            files = localization.get("file_manifest")
            if not isinstance(files, list) or not files:
                raise ScientificExecutionMapError("compiler-cache artifact file identity is absent")
            manifest_sha256 = _digest(
                sorted(
                    (
                        {
                            "path": item.get("path"),
                            "sha256": str(item.get("sha256", "")).removeprefix("sha256:"),
                            "size_bytes": item.get("size_bytes"),
                        }
                        for item in files
                        if isinstance(item, Mapping)
                    ),
                    key=lambda item: str(item["path"]),
                )
            )
        identities.append(
            {
                "artifact_id": artifact_id,
                "content_sha256": str(localization.get("content_digest", "")).removeprefix("sha256:"),
                "manifest_sha256": manifest_sha256.removeprefix("sha256:"),
            }
        )
    try:
        return artifact_set_sha256(identities)
    except ValueError as error:
        raise ScientificExecutionMapError("compiler-cache artifact identity is invalid") from error


def build_execution_map(
    *,
    profiles: ScientificProfileCatalog,
    targets: Mapping[str, Any],
    localizations: Mapping[str, Any],
) -> dict[str, Any]:
    """Expand qualified profiles and deployment bindings into reader-owned v3."""

    if targets.get("schema") != TARGET_SCHEMA or localizations.get("schema") != LOCALIZATION_SCHEMA:
        raise ScientificExecutionMapError("scientific execution generation input schema is unsupported")
    runnable = profiles.list()
    if not runnable:
        raise ScientificExecutionMapError("no qualified scientific profile can be installed")

    raw_localization_models = localizations.get("models")
    if not isinstance(raw_localization_models, list):
        raise ScientificExecutionMapError("scientific runtime localization models are invalid")
    localization_by_model: dict[str, list[dict[str, Any]]] = {}
    for raw_model in raw_localization_models:
        model = _object(raw_model, "scientific runtime localization model")
        model_id = model.get("model_id")
        artifacts = model.get("runtime_artifacts")
        if not isinstance(model_id, str) or model_id in localization_by_model or not isinstance(artifacts, list):
            raise ScientificExecutionMapError("scientific runtime localization model is invalid or duplicated")
        localization_by_model[model_id] = [dict(_object(item, "runtime artifact localization")) for item in artifacts]
    runnable_ids = {profile.model_id for profile in runnable}
    if set(localization_by_model) != runnable_ids:
        raise ScientificExecutionMapError("runtime localizations must cover every runnable model exactly")

    raw_bindings = targets.get("bindings")
    if not isinstance(raw_bindings, list):
        raise ScientificExecutionMapError("scientific execution target bindings are invalid")
    bindings: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw_binding in raw_bindings:
        target_binding = _object(raw_binding, "scientific execution target binding")
        key = (target_binding.get("model_id"), target_binding.get("stage_id"))
        if not all(isinstance(item, str) for item in key) or key in bindings:
            raise ScientificExecutionMapError("scientific execution target binding is invalid or duplicated")
        bindings[cast(tuple[str, str], key)] = target_binding

    default_claim = _object(targets.get("default_runtime_artifact_claim"), "default runtime artifact claim")
    default_namespace = targets.get("default_execution_namespace")
    default_queue = targets.get("default_local_queue_name")
    default_cluster_queue = targets.get("default_cluster_queue_name")
    default_service_account = targets.get("default_service_account_name")
    default_deadline = targets.get("default_active_deadline_seconds")
    default_tolerations = targets.get("default_tolerations")
    if not isinstance(default_tolerations, list):
        raise ScientificExecutionMapError("default tolerations are invalid")

    models: list[dict[str, Any]] = []
    covered_bindings: set[tuple[str, str]] = set()
    for profile in runnable:
        value = profile.value
        identity = _object(value.get("execution_identity"), "profile execution identity")
        workload = _object(value.get("workload"), "profile workload")
        stages = workload.get("stages")
        cancellation = _object(workload.get("cancellation"), "profile cancellation")
        if not isinstance(stages, list):
            raise ScientificExecutionMapError("profile stages are invalid")
        stage_contracts = stage_execution_contracts(profile.model_id)
        stage_ids = {stage.get("id") for stage in stages if isinstance(stage, Mapping)}
        if stage_ids != set(stage_contracts):
            raise ScientificExecutionMapError("adapter stage contract differs from the canonical profile")

        artifacts = localization_by_model[profile.model_id]
        localized = {item.get("artifact_id"): item for item in artifacts}
        requirements = value.get("artifact_requirements")
        if not isinstance(requirements, list):
            raise ScientificExecutionMapError("profile artifact requirements are invalid")
        required_ids = {item.get("artifact_id") for item in requirements if isinstance(item, Mapping)}
        if set(localized) != required_ids or not all(isinstance(item, str) for item in localized):
            raise ScientificExecutionMapError("runtime localization closure differs from the profile")
        for requirement in requirements:
            item = _object(requirement, "profile artifact requirement")
            _validate_localization_requirement(item, localized[cast(str, item["artifact_id"])])

        rendered_stages: list[dict[str, Any]] = []
        for raw_stage in stages:
            stage = _object(raw_stage, "profile stage")
            stage_id = cast(str, stage["id"])
            contract = stage_contracts[stage_id]
            key = (profile.model_id, stage_id)
            binding = bindings.get(key)
            if binding is not None:
                covered_bindings.add(key)
            execution_namespace = binding.get("execution_namespace") if binding else default_namespace
            local_queue = binding.get("local_queue_name") if binding else default_queue
            cluster_queue = binding.get("cluster_queue_name") if binding else default_cluster_queue
            service_account = binding.get("service_account_name") if binding else default_service_account
            placement = _object(stage.get("placement"), "profile stage placement")
            node_selector = dict(_object(placement.get("required_node_labels"), "profile stage node selector"))
            if binding is not None:
                for label, label_value in _object(binding.get("node_selector"), "target node selector").items():
                    if label in node_selector and node_selector[label] != label_value:
                        raise ScientificExecutionMapError("target node selector conflicts with the profile")
                    node_selector[label] = label_value
            raw_mounts = binding.get("mounts", []) if binding else []
            if not isinstance(raw_mounts, list):
                raise ScientificExecutionMapError("target mounts are invalid")
            cache_targets = [item for item in raw_mounts if isinstance(item, Mapping) and item.get("kind") == "cache"]
            if len(cache_targets) > 1:
                raise ScientificExecutionMapError("target stage has more than one compiler cache")
            cache_identity = None
            if cache_targets:
                try:
                    policy = _object(binding.get("compiler_cache") if binding else None, "compiler-cache policy")
                    if policy != {
                        "accelerator_sm": accelerator_sm(cast(dict[str, str], node_selector)),
                        "run_as_user": CACHE_RUN_AS_USER,
                        "run_as_group": CACHE_RUN_AS_GROUP,
                        "sub_path_layout": CACHE_SUB_PATH_LAYOUT,
                        "preparation": CACHE_PREPARATION,
                    }:
                        raise ValueError("compiler-cache policy differs")
                    cache_identity = CompilerCacheIdentity(
                        model_id=profile.model_id,
                        stage_id=stage_id,
                        variant_id=cast(str, value["variant_id"]),
                        runtime_image_digest=cast(str, identity["runtime_image_digest"]),
                        artifact_set_sha256=_cache_artifact_set(
                            contract.runtime_artifacts,
                            cast(Mapping[str, Mapping[str, Any]], localized),
                        ),
                        accelerator_sm=cast(str, policy["accelerator_sm"]),
                        run_as_user=cast(int, policy["run_as_user"]),
                        run_as_group=cast(int, policy["run_as_group"]),
                    )
                except ValueError as error:
                    raise ScientificExecutionMapError(
                        f"{profile.model_id}/{stage_id} compiler-cache identity is invalid"
                    ) from error
            elif binding is not None and binding.get("compiler_cache") is not None:
                raise ScientificExecutionMapError(
                    f"{profile.model_id}/{stage_id} has compiler-cache policy without a cache mount"
                )
            mounts = [dict(WORKSPACE_MOUNT)]
            mounts.extend(
                _target_mount(
                    _object(item, "target mount"),
                    localizations=cast(Mapping[str, Mapping[str, Any]], localized),
                    cache_identity=cache_identity if _object(item, "target mount").get("kind") == "cache" else None,
                )
                for item in raw_mounts
            )
            explicitly_mounted = {mount["artifact_id"] for mount in mounts if mount["artifact_id"] is not None}
            for artifact_id in contract.runtime_artifacts:
                if artifact_id in explicitly_mounted:
                    continue
                if execution_namespace != default_claim.get("namespace"):
                    raise ScientificExecutionMapError(
                        f"{profile.model_id}/{stage_id}/{artifact_id} requires an explicit same-namespace mount"
                    )
                localization = localized[artifact_id]
                mounts.append(
                    {
                        "name": artifact_id,
                        "kind": "reference",
                        "artifact_id": artifact_id,
                        "claim_name": default_claim.get("name"),
                        "claim_namespace": execution_namespace,
                        "host_path": None,
                        "operator_owned": True,
                        "mount_path": localization["mount_path"],
                        "sub_path": artifact_id,
                        "read_only": True,
                        "supplemental_groups": [],
                    }
                )
            resources = _object(stage.get("resources"), "profile stage resources")
            rendered_stages.append(
                {
                    "stage_id": stage_id,
                    "execution_namespace": execution_namespace,
                    "local_queue_name": local_queue,
                    "cluster_queue_name": cluster_queue,
                    "image": f"{identity['runtime_image_repository']}@{identity['runtime_image_digest']}",
                    "collector_id": contract.collector_id,
                    "validator_id": contract.validator_id,
                    "mounts": mounts,
                    "service_account_name": service_account,
                    "resources": {
                        "cpu": _cpu_quantity(resources.get("cpu_millis")),
                        "memory": _gib_quantity(resources.get("memory_bytes"), "profile stage memory request"),
                        "ephemeral_storage": f"{resources.get('ephemeral_storage_request_gib')}Gi",
                    },
                    "active_deadline_seconds": default_deadline,
                    "termination_grace_seconds": cancellation.get("grace_seconds"),
                    "environment": dict(_object(binding.get("environment", {}), "target environment"))
                    if binding
                    else {},
                    "node_selector": node_selector,
                    "tolerations": binding.get("tolerations", default_tolerations) if binding else default_tolerations,
                }
            )
        models.append(
            {
                "model_id": profile.model_id,
                "variant_id": value["variant_id"],
                "execution_identity_sha256": identity["execution_identity_sha256"],
                "plan_adapter": {
                    "module": "fs2_serve.scientific_batch.adapters",
                    "function": "compile_adapter_run",
                },
                "runtime_artifacts": sorted(artifacts, key=lambda item: cast(str, item["artifact_id"])),
                "stages": rendered_stages,
            }
        )
    unknown_bindings = set(bindings) - covered_bindings
    if unknown_bindings and any(model_id in runnable_ids for model_id, _stage_id in unknown_bindings):
        raise ScientificExecutionMapError("execution targets contain an unknown runnable-model stage")

    execution_map = {
        "schema": EXECUTION_SCHEMA,
        "controller_service_account": dict(
            _object(targets.get("controller_service_account"), "controller service account")
        ),
        "models": sorted(models, key=lambda item: cast(str, item["model_id"])),
    }
    # Use the production consumer as the final compiler pass. This catches
    # target policy, artifact, image, namespace, cache and hostPath drift before apply.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fs2-execution-map-") as directory:
        validation_path = Path(directory) / "execution-map.json"
        validation_path.write_bytes(_canonical(execution_map))
        FileScientificManifestRenderer(path=validation_path, profiles=profiles)
    return execution_map


def config_map_manifest(
    execution_map: Mapping[str, Any],
    *,
    profiles_source: Mapping[str, Any],
    targets: Mapping[str, Any],
    localizations: Mapping[str, Any],
    namespace: str = "fs2-system",
) -> dict[str, Any]:
    encoded = _canonical(execution_map).decode()
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"fs2-scientific-execution-{digest[:12]}",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "terraform",
                "app.kubernetes.io/part-of": "fs2-serve",
                "app.kubernetes.io/component": "scientific-execution",
            },
            "annotations": {
                "fs2.nebius.ai/execution-map-sha256": digest,
                "fs2.nebius.ai/scientific-profiles-sha256": _digest(profiles_source),
                "fs2.nebius.ai/scientific-targets-sha256": _digest(targets),
                "fs2.nebius.ai/runtime-localizations-sha256": _digest(localizations),
            },
        },
        "immutable": True,
        "data": {"execution-map.json": encoded},
    }


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("input exceeds 8 MiB")
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ScientificExecutionMapError(f"{label} is unavailable or invalid") from error
    return dict(_object(value, label))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument(
        "--targets",
        type=Path,
        help="defaults to <catalog-root>/contracts/scientific-execution-targets.json",
    )
    parser.add_argument("--localizations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config-map-output", type=Path)
    parser.add_argument("--namespace", default="fs2-system")
    args = parser.parse_args()
    catalog_root = cast(Path, args.catalog_root)
    targets_path = (
        cast(Path, args.targets)
        if args.targets is not None
        else catalog_root / "contracts/scientific-execution-targets.json"
    )
    targets = _read_json(targets_path, "scientific execution targets")
    localizations = _read_json(cast(Path, args.localizations), "scientific runtime localizations")
    schema_root = catalog_root / "schema"
    target_schema = _read_json(schema_root / "scientific-execution-targets.schema.json", "target schema")
    Draft202012Validator(target_schema).validate(targets)
    Draft202012Validator(
        _read_json(schema_root / "scientific-runtime-localizations.schema.json", "localization schema")
    ).validate(localizations)
    profiles_source = _read_json(
        catalog_root / "contracts/scientific-workload-profiles.json", "scientific workload profiles"
    )
    profiles = ScientificProfileCatalog.load(catalog_root)
    execution_map = build_execution_map(profiles=profiles, targets=targets, localizations=localizations)
    output = cast(Path, args.output)
    output.write_text(json.dumps(execution_map, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.config_map_output is not None:
        manifest = config_map_manifest(
            execution_map,
            profiles_source=profiles_source,
            targets=targets,
            localizations=localizations,
            namespace=cast(str, args.namespace),
        )
        cast(Path, args.config_map_output).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
