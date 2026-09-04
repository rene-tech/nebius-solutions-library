#!/usr/bin/env python3
"""Execute the real four-image adapter tuples against the image wrappers.

AlphaFold3 is deliberately absent. Its image/runtime boundary and academic
reference-data admission are owned and verified by the dedicated clean AF3
successor, not by this publisher.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
RAW_SHA256 = "a" * 64
MINIMUM_REPAIR_REVISION = "cd4069927a447f21bee2b538bb9edb5c4c38266c"
MODEL_CONTRACTS = {
    "esmfold2": ("models/structure/batch-adapters/esmfold2/contract.json", "esmfold2"),
    "esmfold2-fast": (
        "models/structure/batch-adapters/esmfold2-fast/contract.json",
        "esmfold2-fast",
    ),
    "protenix-v2": (
        "models/structure/batch-adapters/protenix-v2/contract.json",
        "protenix-v2",
    ),
    "openfold3": (
        "models/structure/batch-adapters/openfold3/contract.json",
        "openfold3-openbind",
    ),
}
HANDOFF_PATH = "models/structure/batch-adapters/secondary-r4-image-handoff.json"
RUNTIME_BYTE_FILES = (
    "python-runtime-launcher.sh",
    "run_esmfold2.py",
    "run_protenix.py",
    "run_openfold3.py",
    "runtime_localization.py",
    "handoff_contract.py",
    "result_contract.py",
    "confidence.schema.json",
    "openfold3-runner-base.yaml",
)
CACHE_ENVIRONMENT_KEYS = frozenset(
    {
        "TRITON_CACHE_DIR",
        "CUEQ_TRITON_CACHE_DIR",
        "TORCH_EXTENSIONS_DIR",
        "XDG_CACHE_HOME",
    }
)


def _git(worktree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _load_wrapper(name: str):
    spec = importlib.util.spec_from_file_location(
        f"fs2_image_{name}", ROOT / f"{name}.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load image wrapper {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not readable JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain one JSON object: {path}")
    return value


def _load_model_contracts(worktree: Path) -> dict[str, dict[str, object]]:
    root = worktree / "k8s-inference"
    contracts: dict[str, dict[str, object]] = {}
    for image_id, (relative, public_model_id) in MODEL_CONTRACTS.items():
        contract = _read_object(root / relative, label=f"{image_id} adapter contract")
        if (
            contract.get("schema")
            != "fs2-serve.nebius.ai/scientific-adapter-identity/v1"
            or contract.get("model_id") != public_model_id
        ):
            raise RuntimeError(f"{image_id} model-owned adapter contract identity is invalid")
        contracts[image_id] = contract
    return contracts


def _candidate_profile_from_contract(contract: dict[str, object]) -> dict[str, object]:
    """Build only the fail-closed catalog seam needed to compile real argv.

    This is deliberately not a shared runtime profile or activation document.
    Every identity and stage bound here originates in the model-owned adapter
    contract from the exact adapter commit under review.
    """

    model_id = contract.get("model_id")
    source = contract.get("source")
    interface = contract.get("interface")
    stages = contract.get("stages")
    if (
        not isinstance(model_id, str)
        or not isinstance(source, dict)
        or not isinstance(interface, dict)
        or not isinstance(stages, list)
        or not stages
    ):
        raise RuntimeError(f"{model_id!r} model-owned adapter contract is incomplete")
    operations = interface.get("operations")
    if not isinstance(operations, list) or not operations or not all(
        isinstance(item, str) for item in operations
    ):
        raise RuntimeError(f"{model_id} model-owned adapter operations are invalid")
    profile_stages: list[dict[str, object]] = []
    previous: str | None = None
    for raw in stages:
        if not isinstance(raw, dict):
            raise RuntimeError(f"{model_id} model-owned stage is not an object")
        stage_id = raw.get("stage_id")
        resource_class = raw.get("resource_class")
        maximum = raw.get("max_parallelism")
        if (
            not isinstance(stage_id, str)
            or resource_class not in {"cpu", "gpu"}
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or maximum < 1
        ):
            raise RuntimeError(f"{model_id} model-owned stage bounds are invalid")
        profile_stages.append(
            {
                "id": stage_id,
                "needs": [] if previous is None else [previous],
                "resource_class": resource_class,
                "admission_mode": "independent-jobs",
                "min_parallelism": 1,
                "max_parallelism": maximum,
                "checkpoint_mode": "none" if resource_class == "cpu" else "restart",
                "preemption_mode": (
                    "non_preemptible" if resource_class == "cpu" else "restartable"
                ),
            }
        )
        previous = stage_id
    return {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": model_id,
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": "git",
            "repository": source.get("repository"),
            "revision": source.get("revision"),
            "review_url": "https://invalid.fs2.local/build-only-contract",
            "classification": "candidate-source",
        },
        "interface": {
            "protocol": "staged-batch-v1",
            "submit_endpoint": "/v1/scientific/runs",
            "request_schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "parameter_schema": interface.get("parameter_schema"),
            "operations": operations,
            "service_classes": ["customer-batch"],
            "mcp": False,
        },
        "resources": {
            "gpu_count": 1,
            "gpu_topology": "single-gpu",
            "host_architectures": ["amd64"],
            "compatible_pool_ids": [],
            "required_node_labels": {},
        },
        "workload": {
            "stages": profile_stages,
            "retry": {"max_attempts": 1},
        },
    }


def _request(contract: dict[str, object], parameters: dict[str, object]) -> dict[str, object]:
    interface = contract.get("interface")
    operations = interface.get("operations") if isinstance(interface, dict) else None
    if not isinstance(operations, list) or not operations or not isinstance(operations[0], str):
        raise RuntimeError("model-owned adapter contract has no operation")
    return {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": operations[0],
        "service_class": "customer-batch",
        "input_manifest": {
            "artifact_id": "00000000-0000-4000-8000-000000000001",
            "sha256": "c" * 64,
            "size_bytes": 100,
            "media_type": "application/vnd.fs2.scientific-manifest+json",
            "compression": "none",
        },
        "parameters": parameters,
    }


def _argument(argv: tuple[str, ...], name: str) -> str:
    if argv.count(name) != 1:
        raise RuntimeError(f"generated argv must contain {name} exactly once: {argv}")
    return argv[argv.index(name) + 1]


def _execute_parser(wrapper, invocation):
    command = invocation.argv[1]
    handlers = {
        "prepare": "_prepare",
        "predict": "_predict",
        "prep": "_prep",
        "pred": "_pred",
        "prepare-input": "_prepare",
        "fold": "_fold",
    }
    handler = handlers.get(command)
    if handler is None:
        raise RuntimeError(f"unrecognized generated image subcommand: {command}")
    with mock.patch.object(wrapper, handler) as parsed_handler:
        if len(inspect.signature(wrapper.main).parameters) == 0:
            with mock.patch.object(
                sys, "argv", [str(invocation.argv[0]), *invocation.argv[1:]]
            ):
                wrapper.main()
        else:
            wrapper.main(list(invocation.argv[1:]))
        if parsed_handler.call_count != 1:
            raise RuntimeError(
                f"actual wrapper parser did not dispatch generated {command} argv"
            )
        return parsed_handler.call_args.args[0]


def _execute_runtime_marker(
    wrapper,
    invocation,
    parsed_args,
    *,
    model_id: str,
    variant_id: str,
    artifacts: dict[str, dict[str, object]],
    destination: Path,
) -> None:
    marker_path = destination / f"{model_id}-{invocation.stage_id}.json"
    receipt = "d" * 64
    marker = {
        "schema": "fs2-serve.nebius.ai/runtime-localization-marker/v1",
        "operation_id": "00000000-0000-4000-8000-000000000010",
        "attempt_id": "00000000-0000-4000-8000-000000000011",
        "tenant_id": "adapter-contract-tenant",
        "model_id": model_id,
        "variant_id": variant_id,
        "stage_id": invocation.stage_id,
        "artifacts": [
            {
                "artifact_id": artifact_id,
                "mount_path": artifacts[artifact_id]["mount_path"],
                "content_digest": f"sha256:{artifacts[artifact_id]['content_sha256']}",
                "localization_receipt_digest": f"sha256:{receipt}",
                "sub_path": artifacts[artifact_id].get("sub_path"),
                "expected_manifest_sha256": artifacts[artifact_id].get(
                    "localization_manifest_sha256"
                ),
                "readiness_receipt_sha256": receipt,
                "authorization_receipt_sha256": None,
            }
            for artifact_id in invocation.runtime_artifacts
        ],
    }
    marker_path.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    parsed_args.runtime_localization_marker = str(marker_path)
    environment = {
        "FS2_OPERATION_ID": marker["operation_id"],
        "FS2_ATTEMPT_ID": marker["attempt_id"],
        "FS2_TENANT_ID": marker["tenant_id"],
        "FS2_VARIANT_ID": variant_id,
        "FS2_STAGE_ID": invocation.stage_id,
        "FS2_RUNTIME_LOCALIZATION_MARKER": str(marker_path),
        "FS2_ARTIFACT_ACCESS_RECEIPT_DIGEST": "",
    }
    with mock.patch.dict(os.environ, environment, clear=False):
        wrapper._validate_runtime_localization_args(invocation.argv[1], parsed_args)


def _compile(module, profile, request, *, model_id: str):
    signature = inspect.signature(module.compile_run)
    if tuple(signature.parameters) != (
        "profile",
        "request_value",
        "operation_id",
        "input_artifacts",
    ):
        raise RuntimeError(
            f"{model_id} adapter compile_run has an unexpected public signature"
        )
    from fs2_serve.scientific_batch.models import ScientificInputArtifact

    model_input = ScientificInputArtifact(
        logical_artifact_id=module.INPUT_ARTIFACT_ID,
        semantic_type=module.INPUT_SEMANTIC_TYPE,
        artifact_id=UUID("00000000-0000-4000-8000-000000000002"),
        digest=f"sha256:{RAW_SHA256}",
        size_bytes=100,
        media_type=module.INPUT_MEDIA_TYPE,
        compression="none",
    )
    return module.compile_run(
        profile,
        request,
        operation_id=f"adapter-contract-{model_id}",
        input_artifacts=(model_input,),
    )


def _contract_artifacts(
    contract: dict[str, object], *, model_id: str
) -> dict[str, dict[str, object]]:
    raw = contract.get("runtime_artifacts")
    if not isinstance(raw, list):
        raise RuntimeError(f"{model_id} model-owned runtime_artifacts must be an array")
    artifacts: dict[str, dict[str, object]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str):
            raise RuntimeError(f"{model_id} model-owned runtime artifact is invalid")
        artifact_id = item["artifact_id"]
        if artifact_id in artifacts:
            raise RuntimeError(f"{model_id} model-owned runtime artifact is duplicated")
        if not isinstance(item.get("mount_path"), str) or not isinstance(
            item.get("content_sha256"), str
        ):
            raise RuntimeError(
                f"{model_id} model-owned runtime artifact {artifact_id} lacks identity"
            )
        artifacts[artifact_id] = item
    return artifacts


def _lock_content_sha256(item: dict[str, object]) -> object:
    return item.get("content_digest", item.get("localized_content_digest_sha256"))


def _validate_contract_and_plan(
    image_id: str,
    module,
    contract: dict[str, object],
    image: dict[str, object],
    handoff_image: dict[str, object],
    plan,
    failures: list[str],
) -> dict[str, object]:
    source = contract.get("source")
    interface = contract.get("interface")
    runtime_image = contract.get("runtime_image")
    activation = contract.get("activation")
    stages = contract.get("stages")
    if not all(
        isinstance(value, dict)
        for value in (source, interface, runtime_image, activation)
    ) or not isinstance(stages, list):
        failures.append(f"{image_id} model-owned contract is incomplete")
        return {}
    public_model_id = contract.get("model_id")
    if (
        module.MODEL_ID != public_model_id
        or module.VARIANT_ID != contract.get("variant_id")
        or module.SOURCE_REPOSITORY != source.get("repository")
        or module.SOURCE_REVISION != source.get("revision")
        or module.PARAMETER_SCHEMA != interface.get("parameter_schema")
    ):
        failures.append(f"{image_id} adapter module differs from its model-owned contract")
    if image.get("source", {}).get("revision") != source.get("revision"):
        failures.append(f"{image_id} image source revision differs from the adapter contract")

    repository = runtime_image.get("repository")
    relative_repository = image.get("repository")
    repository_suffix = f"/{relative_repository}"
    if not isinstance(repository, str) or not repository.endswith(repository_suffix):
        failures.append(f"{image_id} runtime image repository differs from the image lock")
        registry_root = None
    else:
        registry_root = repository[: -len(repository_suffix)]
    published_digest = image.get("published_digest")
    for field in ("tag", "digest"):
        lock_field = "published_digest" if field == "digest" else field
        if handoff_image.get(field) != image.get(lock_field):
            failures.append(f"{image_id} handoff image {field} differs from the image lock")
        if published_digest is not None and runtime_image.get(field) != image.get(lock_field):
            failures.append(f"{image_id} runtime image {field} differs from the image lock")
    if handoff_image.get("repository") != repository:
        failures.append(f"{image_id} handoff repository differs from its contract")
    if (
        runtime_image.get("state") != "build-only-not-semantic-qualified"
        or activation.get("profile_state") != "candidate-unqualified"
        or activation.get("route_exposed") is not False
        or activation.get("semantic_h100_qualified") is not False
        or image.get("deployable") is not False
    ):
        failures.append(f"{image_id} contract overstates activation or deployability")

    entrypoint = interface.get("entrypoint")
    subcommands = interface.get("subcommands")
    if (
        entrypoint != image.get("runtime_contract", {}).get("entrypoint")
        or subcommands != image.get("runtime_contract", {}).get("subcommands")
    ):
        failures.append(f"{image_id} CLI identity differs from the image lock")
    actual_stage_ids = [item.stage_id for item in plan.invocations]
    contract_stage_ids = [
        item.get("stage_id") if isinstance(item, dict) else None for item in stages
    ]
    if actual_stage_ids != contract_stage_ids:
        failures.append(f"{image_id} generated stages differ from its model-owned contract")

    artifacts = _contract_artifacts(contract, model_id=image_id)
    declared_by_stage: dict[str, tuple[str, ...]] = {}
    for raw_stage in stages:
        if not isinstance(raw_stage, dict) or not isinstance(raw_stage.get("stage_id"), str):
            continue
        raw_artifacts = raw_stage.get("runtime_artifacts")
        if not isinstance(raw_artifacts, list) or not all(
            isinstance(item, str) for item in raw_artifacts
        ):
            failures.append(f"{image_id} stage runtime_artifacts are invalid")
            continue
        declared_by_stage[raw_stage["stage_id"]] = tuple(raw_artifacts)
    for invocation in plan.invocations:
        if invocation.argv[0] != entrypoint or invocation.argv[1] not in (subcommands or []):
            failures.append(f"{image_id} generated argv escapes its wrapper contract")
        if invocation.runtime_artifacts != declared_by_stage.get(invocation.stage_id):
            failures.append(
                f"{image_id}/{invocation.stage_id} runtime_artifacts seam differs from contract"
            )
    module_stages = getattr(module, "STAGE_EXECUTION_CONTRACTS", {})
    for stage_id, declared in declared_by_stage.items():
        module_stage = module_stages.get(stage_id)
        if not isinstance(module_stage, dict) and not hasattr(module_stage, "get"):
            failures.append(f"{image_id}/{stage_id} module stage contract is absent")
            continue
        contract_stage = next(
            item for item in stages if isinstance(item, dict) and item.get("stage_id") == stage_id
        )
        if (
            tuple(module_stage.get("runtime_artifacts", ())) != declared
            or module_stage.get("collector_id") != contract_stage.get("collector_id")
            or module_stage.get("validator_id") != contract_stage.get("validator_id")
        ):
            failures.append(f"{image_id}/{stage_id} module stage identity differs")

    lock_artifacts = {
        item.get("id"): item
        for item in image.get("external_artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    observed = {
        artifact_id
        for invocation in plan.invocations
        for artifact_id in invocation.runtime_artifacts
    }
    if observed != set(artifacts) or set(lock_artifacts) != set(artifacts):
        failures.append(f"{image_id} runtime artifact closure differs across contracts")
    for artifact_id, artifact in artifacts.items():
        lock_artifact = lock_artifacts.get(artifact_id, {})
        if (
            lock_artifact.get("mount") != artifact.get("mount_path")
            or _lock_content_sha256(lock_artifact) != artifact.get("content_sha256")
            or lock_artifact.get("runtime_path") != artifact.get("runtime_path")
        ):
            failures.append(f"{image_id} artifact {artifact_id} differs from the image lock")
        if artifact.get("localization_manifest_sha256") is not None and (
            lock_artifact.get("localization_manifest_sha256")
            != artifact.get("localization_manifest_sha256")
        ):
            failures.append(f"{image_id} artifact {artifact_id} manifest digest differs")
        if artifact.get("file_sha256") is not None and (
            lock_artifact.get("sha256") != artifact.get("file_sha256")
        ):
            failures.append(f"{image_id} artifact {artifact_id} file digest differs")
    return {
        "registry_root": registry_root,
        "artifact_contracts": artifacts,
        "runtime_artifacts_seam": declared_by_stage,
    }


def _validate_cache_contract(
    model_id: str,
    contract: dict[str, object],
    image: dict[str, object],
    plan,
    failures: list[str],
) -> dict[str, object]:
    declared = contract.get("runtime_cache")
    image_runtime = image.get("runtime_contract")
    if not isinstance(image_runtime, dict):
        failures.append(f"{model_id} image runtime contract is absent")
        return {}
    if declared is None:
        if image_runtime.get("writable_cache_mounts") or image_runtime.get(
            "cache_environment"
        ):
            failures.append(f"{model_id} image lock has an undeclared compiler cache")
        for invocation in plan.invocations:
            if CACHE_ENVIRONMENT_KEYS.intersection(dict(invocation.environment)):
                failures.append(f"{model_id} generated argv has an undeclared cache environment")
        return {
            "declared": False,
            "delivery_state": "not-required",
            "activation_qualified": False,
        }
    if not isinstance(declared, dict):
        failures.append(f"{model_id} runtime_cache contract is invalid")
        return {}
    mount_path = declared.get("mount_path")
    environment = declared.get("environment")
    if (
        image_runtime.get("writable_cache_mounts") != [mount_path]
        or image_runtime.get("cache_environment") != environment
        or declared.get("qualified_level") != "Off"
        or not isinstance(environment, dict)
        or not environment
    ):
        failures.append(f"{model_id} cache declaration differs from the image contract")
        return {
            "declared": True,
            "delivery_state": "pending-external-activation",
            "activation_qualified": False,
        }
    exact_environment_stages: list[str] = []
    for invocation in plan.invocations:
        invocation_environment = dict(invocation.environment)
        observed = {
            key: invocation_environment[key]
            for key in CACHE_ENVIRONMENT_KEYS
            if key in invocation_environment
        }
        if observed and observed != environment:
            failures.append(f"{model_id}/{invocation.stage_id} cache environment differs")
        if observed == environment:
            exact_environment_stages.append(invocation.stage_id)
    if not exact_environment_stages:
        failures.append(f"{model_id} generated plan never binds its compiler-cache environment")
    return {
        "declared": True,
        "mount_path": mount_path,
        "environment": environment,
        "generated_environment_stages": exact_environment_stages,
        "delivery_state": "pending-external-activation",
        "activation_qualified": False,
    }


def _validate_committed_runtime_bytes(
    repo: Path, source_revision: str, failures: list[str]
) -> dict[str, object]:
    runtime_root = ROOT.relative_to(repo)
    evidence: dict[str, object] = {}
    for filename in RUNTIME_BYTE_FILES:
        relative = (runtime_root / filename).as_posix()
        current = (ROOT / filename).read_bytes()
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{source_revision}:{relative}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            failures.append(f"runtime source commit is unavailable: {relative}")
            continue
        current_sha256 = hashlib.sha256(current).hexdigest()
        committed_sha256 = hashlib.sha256(result.stdout).hexdigest()
        if current != result.stdout:
            failures.append(f"runtime bytes differ from image source commit: {relative}")
        evidence[filename] = {
            "sha256": current_sha256,
            "committed_sha256": committed_sha256,
            "matches_image_source": current == result.stdout,
        }
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter-worktree", type=Path, required=True)
    args = parser.parse_args()
    worktree = args.adapter_worktree.resolve()
    control_plane = worktree / "k8s-inference/components/control-plane/src"
    if not control_plane.is_dir():
        raise SystemExit("adapter-worktree does not contain the runtime control-plane")

    revision = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    dirty = _git(worktree, "status", "--porcelain").stdout
    branch = _git(worktree, "branch", "--show-current").stdout.strip()
    branch_remote = (
        _git(worktree, "config", "--get", f"branch.{branch}.remote", check=False)
        .stdout.strip()
        if branch
        else ""
    )
    branch_merge_ref = (
        _git(worktree, "config", "--get", f"branch.{branch}.merge", check=False)
        .stdout.strip()
        if branch
        else ""
    )
    if not branch_remote and branch:
        branch_remote = "origin"
    if not branch_merge_ref and branch:
        branch_merge_ref = f"refs/heads/{branch}"
    remote_revision = ""
    if branch_remote and branch_merge_ref:
        remote_result = _git(
            worktree,
            "ls-remote",
            "--exit-code",
            branch_remote,
            branch_merge_ref,
            check=False,
        )
        if remote_result.returncode == 0 and remote_result.stdout.strip():
            remote_revision = remote_result.stdout.split()[0]
    task_repo = Path(_git(ROOT, "rev-parse", "--show-toplevel").stdout.strip())
    task_revision = _git(task_repo, "rev-parse", "HEAD").stdout.strip()
    current_main = _git(task_repo, "rev-parse", "origin/main").stdout.strip()
    main_integrated_into_task = (
        _git(
            worktree,
            "merge-base",
            "--is-ancestor",
            current_main,
            revision,
            check=False,
        ).returncode
        == 0
    )
    repair_is_ancestor = (
        _git(
            worktree,
            "merge-base",
            "--is-ancestor",
            MINIMUM_REPAIR_REVISION,
            revision,
            check=False,
        ).returncode
        == 0
    )

    sys.path.insert(0, str(control_plane))
    sys.path.insert(0, str(ROOT))
    from fs2_serve.scientific_batch.adapters import (
        esmfold2,
        esmfold2_fast,
        openfold3,
        protenix_v2,
    )

    modules = {
        "esmfold2": esmfold2,
        "esmfold2-fast": esmfold2_fast,
        "protenix-v2": protenix_v2,
        "openfold3": openfold3,
    }
    contracts = _load_model_contracts(worktree)
    profiles = {
        model_id: _candidate_profile_from_contract(contract)
        for model_id, contract in contracts.items()
    }
    image_lock = _read_object(ROOT / "image-lock.json", label="image lock")
    raw_images = image_lock.get("images")
    if not isinstance(raw_images, list):
        raise SystemExit("image lock images must be an array")
    images = {
        item.get("id"): item
        for item in raw_images
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    handoff = _read_object(
        worktree / "k8s-inference" / HANDOFF_PATH,
        label="secondary successor image handoff",
    )
    raw_handoff_images = handoff.get("images")
    if not isinstance(raw_handoff_images, list):
        raise SystemExit("secondary successor image handoff images must be an array")
    public_to_image = {
        public_model_id: image_id
        for image_id, (_, public_model_id) in MODEL_CONTRACTS.items()
    }
    handoff_images = {}
    for item in raw_handoff_images:
        if not isinstance(item, dict) or not isinstance(item.get("model_id"), str):
            continue
        image_id = public_to_image.get(item["model_id"])
        if image_id is not None:
            handoff_images[image_id] = item
    parameters = {
        "esmfold2": {"sequence": "ACDEFGHIK", "mode": "single-sequence", "seed": 11},
        "esmfold2-fast": {
            "sequence": "ACDEFGHIK",
            "mode": "single-sequence",
            "seed": 11,
        },
        "protenix-v2": {
            "checkpoint": "protenix-v2",
            "msa_mode": "none",
            "sample_count": 2,
            "model_seeds": [11, 29],
        },
        "openfold3": {"model_seeds": [11, 29], "msa_mode": "none"},
    }
    failures: list[str] = []
    if revision != task_revision:
        failures.append(
            "adapter worktree and image source are not the same exact commit "
            f"(image={task_revision}, adapter={revision})"
        )
    if not repair_is_ancestor:
        failures.append(
            "adapter/image source does not contain the accepted production repair "
            f"{MINIMUM_REPAIR_REVISION}"
        )
    if dirty:
        failures.append("adapter worktree is dirty; evidence needs a concrete commit")
    if remote_revision != revision:
        failures.append(
            "adapter commit is not the exact clean pushed branch head "
            f"(local={revision}, remote={remote_revision or 'unavailable'})"
        )
    if not main_integrated_into_task:
        failures.append(
            "image task does not contain current origin/main "
            f"{current_main}; integrate main before publishing the successor"
        )
    published_digests = [image.get("published_digest") for image in images.values()]
    all_pending = bool(published_digests) and all(value is None for value in published_digests)
    all_published = bool(published_digests) and all(
        isinstance(value, str) and value.startswith("sha256:")
        for value in published_digests
    )
    some_pending = any(value is None for value in published_digests)
    some_published = any(
        isinstance(value, str) and value.startswith("sha256:")
        for value in published_digests
    )
    image_source_revision = handoff.get("image_source_commit")
    if (
        handoff.get("schema") != "fs2.nebius.ai/secondary-successor-image-handoff/v1"
        or handoff.get("semantic_h100_qualification") is not False
        or handoff.get("route_activation_allowed") is not False
        or set(handoff_images) != set(modules)
    ):
        failures.append("secondary successor handoff overstates or differs from closed scope")
    if all_pending:
        if (
            handoff.get("state") != "publication-pending-not-activated"
            or image_source_revision is not None
            or handoff.get("production_protocol_compatible") is not False
        ):
            failures.append("pending successor handoff claims a publication source")
        image_source_revision = task_revision
    elif all_published:
        if (
            handoff.get("state") != "published-build-only-not-activated"
            or not isinstance(image_source_revision, str)
            or len(image_source_revision) != 40
            or handoff.get("production_protocol_compatible") is not True
        ):
            failures.append("published successor handoff lacks its exact image source")
            image_source_revision = task_revision
        elif _git(
            task_repo,
            "merge-base",
            "--is-ancestor",
            image_source_revision,
            task_revision,
            check=False,
        ).returncode != 0:
            failures.append("published image source is not an ancestor of current evidence")
    elif some_pending and some_published:
        if (
            handoff.get("state") != "publication-partial-pending-not-activated"
            or not isinstance(image_source_revision, str)
            or len(image_source_revision) != 40
            or handoff.get("production_protocol_compatible") is not False
        ):
            failures.append("partial successor publication handoff is inconsistent")
        elif _git(
            task_repo,
            "merge-base",
            "--is-ancestor",
            image_source_revision,
            task_revision,
            check=False,
        ).returncode != 0:
            failures.append("published predecessor source is not an ancestor of current evidence")
        image_source_revision = task_revision
    else:
        failures.append("successor image lock mixes pending and published identities")
        image_source_revision = task_revision
    if set(images) != set(modules):
        failures.append("image lock does not contain exactly the four task-owned models")

    plans: dict[str, object] = {}
    for model_id, module in modules.items():
        try:
            plans[model_id] = _compile(
                module,
                profiles[model_id],
                _request(contracts[model_id], parameters[model_id]),
                model_id=model_id,
            )
        except Exception as exc:  # noqa: BLE001 - aggregate all lane failures
            failures.append(
                f"{model_id} actual adapter compilation failed: "
                f"{type(exc).__name__}: {exc}"
            )

    wrappers = {
        "esmfold2": _load_wrapper("run_esmfold2"),
        "esmfold2-fast": _load_wrapper("run_esmfold2"),
        "protenix-v2": _load_wrapper("run_protenix"),
        "openfold3": _load_wrapper("run_openfold3"),
    }
    evidence: dict[str, object] = {
        "adapter_revision": revision,
        "image_task_revision": task_revision,
        "minimum_repair_revision": MINIMUM_REPAIR_REVISION,
        "repair_is_ancestor": repair_is_ancestor,
        "adapter_branch": branch,
        "adapter_remote_revision": remote_revision or None,
        "adapter_is_exact_pushed_head": remote_revision == revision,
        "required_main_revision": current_main,
        "required_main_integrated_into_image_task": main_integrated_into_task,
        "image_source_revision": image_source_revision,
        "profile_source": "model-owned-contract-derived-candidate-fixture",
        "shared_profile_required": False,
        "shared_execution_target_required": False,
        "localization_delivery_state": "pending-external-activation",
        "committed_runtime_bytes": _validate_committed_runtime_bytes(
            task_repo, image_source_revision, failures
        ),
        "models": {},
    }
    with tempfile.TemporaryDirectory() as temporary:
        marker_root = Path(temporary)
        for model_id, plan in plans.items():
            validated_stages: list[str] = []
            model_contract_evidence = _validate_contract_and_plan(
                model_id,
                modules[model_id],
                contracts[model_id],
                images.get(model_id, {}),
                handoff_images.get(model_id, {}),
                plan,
                failures,
            )
            cache_evidence = _validate_cache_contract(
                model_id,
                contracts[model_id],
                images.get(model_id, {}),
                plan,
                failures,
            )
            contract_artifacts = _contract_artifacts(
                contracts[model_id], model_id=model_id
            )
            for invocation in plan.invocations:
                try:
                    parsed_args = _execute_parser(wrappers[model_id], invocation)
                    if invocation.runtime_artifacts:
                        _execute_runtime_marker(
                            wrappers[model_id],
                            invocation,
                            parsed_args,
                            model_id=str(contracts[model_id]["model_id"]),
                            variant_id=plan.variant_id,
                            artifacts=contract_artifacts,
                            destination=marker_root,
                        )
                        validated_stages.append(invocation.stage_id)
                except BaseException as exc:  # argparse and wrappers raise SystemExit
                    failures.append(
                        f"{model_id} {invocation.stage_id} actual argv/marker failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            evidence["models"][model_id] = {
                "contract": model_contract_evidence,
                "cache": cache_evidence,
                "argv": [list(item.argv) for item in plan.invocations],
                "validated_runtime_localization_marker_stages": validated_stages,
                "runtime_artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "mount_path": contract_artifacts[artifact_id]["mount_path"],
                        "expected_content_sha256": contract_artifacts[artifact_id][
                            "content_sha256"
                        ],
                        "artifact_manifest_sha256": contract_artifacts[
                            artifact_id
                        ].get("localization_manifest_sha256"),
                    }
                    for invocation in plan.invocations
                    for artifact_id in invocation.runtime_artifacts
                ],
            }

    if "openfold3" in plans:
        prepare = plans["openfold3"].invocations[0]
        if getattr(prepare, "runtime_artifacts", ()) or any(
            value in prepare.argv
            for value in ("--ccd-path", "--db-dir", "--database-dir")
        ):
            failures.append("OpenFold prepare has an illegal reference-data dependency")
    if "protenix-v2" in plans:
        prediction = plans["protenix-v2"].invocations[-1]
        try:
            if (
                _argument(prediction.argv, "--seeds") != "11,29"
                or "--seed" in prediction.argv
            ):
                failures.append("Protenix argv is not the ordered multi-seed CSV surface")
        except RuntimeError as exc:
            failures.append(str(exc))

    evidence["failures"] = failures
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    if failures:
        raise SystemExit("; ".join(failures))


if __name__ == "__main__":
    main()
