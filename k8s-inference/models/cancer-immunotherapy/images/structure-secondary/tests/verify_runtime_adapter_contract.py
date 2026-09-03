#!/usr/bin/env python3
"""Execute the real four-image adapter tuples against the image wrappers.

AlphaFold3 is deliberately absent. Its image/runtime boundary and academic
reference-data admission are owned and verified by the dedicated clean AF3
successor, not by this publisher.
"""

from __future__ import annotations

import argparse
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
PROTENIX_CONTENT_SHA256 = (
    "5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48"
)
PROTENIX_MANIFEST_SHA256 = (
    "a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7"
)
EXPECTED_REPOSITORIES = {
    "esmfold2": "cancer-immunotherapy/esmfold2",
    "esmfold2-fast": "cancer-immunotherapy/esmfold2-fast",
    "protenix-v2": "cancer-immunotherapy/protenix-v2",
    "openfold3": "cancer-immunotherapy/openfold3-upstream",
}
EXPECTED_ARTIFACTS = {
    "esmfold2": {
        "esmfold2-trunk": (
            "136a3580c01cc055ae5a1278bae056e5150a5441ddb89dfbafb9f4e88d763a0c",
            "/models/esmfold2",
        ),
        "esmc-6b": (
            "8f21da30919b3e0d7af9ec6c4b9879542234d77d42ce061fef029397a4d39758",
            "/models/esmc-6b",
        ),
        "esmfold2-ccd": (
            "b1c2fe19204c57f7a7cca6ab4cb0cb420b99312fff424ef2e405fc8234b7616e",
            "/databases/esmfold2",
        ),
    },
    "esmfold2-fast": {
        "esmfold2-fast-trunk": (
            "19ceaffb5860acf160ea199599fb719b0566519e4cc2fa7a7aa5ef547942ad63",
            "/models/esmfold2-fast",
        ),
        "esmc-6b": (
            "8f21da30919b3e0d7af9ec6c4b9879542234d77d42ce061fef029397a4d39758",
            "/models/esmc-6b",
        ),
        "esmfold2-ccd": (
            "b1c2fe19204c57f7a7cca6ab4cb0cb420b99312fff424ef2e405fc8234b7616e",
            "/databases/esmfold2",
        ),
    },
    "protenix-v2": {
        "protenix-v2": (PROTENIX_CONTENT_SHA256, "/models/protenix-v2"),
    },
    "openfold3": {
        "openfold3-openbind-0": (
            "f954e2f2e3d0bdba297ac8009f6d590b3e2c28ca2985742c9bbd8167f276f6b5",
            "/models/openfold3",
        ),
        "openfold3-components-bcif": (
            "ff75f66793c11d7cb63531c758b210fa6fe33d5a39378bb0ab89094278e95e3b",
            "/databases/openfold3",
        ),
    },
}
EXPECTED_DEPLOYMENT_CACHES = {
    ("protenix-v2", "sample-structure"): {
        "mount_path": "/cache/protenix",
        "environment": {
            "TRITON_CACHE_DIR": "/cache/protenix/triton",
            "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
            "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
            "XDG_CACHE_HOME": "/cache/protenix/xdg",
        },
    },
    ("openfold3", "inference"): {
        "mount_path": "/cache/openfold3",
        "environment": {
            "TRITON_CACHE_DIR": "/cache/openfold3/triton",
            "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
            "XDG_CACHE_HOME": "/cache/openfold3/xdg",
        },
    },
}


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


def _profile(profiles: list[object], model_id: str) -> dict[str, object]:
    matches = [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("model_id") == model_id
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one actual adapter profile for {model_id}, got {len(matches)}"
        )
    return matches[0]


def _request(profile: dict[str, object], parameters: dict[str, object]) -> dict[str, object]:
    interface = profile.get("interface")
    operations = interface.get("operations") if isinstance(interface, dict) else None
    if not isinstance(operations, list) or not operations or not isinstance(operations[0], str):
        raise RuntimeError("actual adapter profile has no operation")
    return {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": operations[0],
        "service_class": "customer-batch",
        "input_manifest": {
            "artifact_id": "adapter-contract-manifest",
            "sha256": "c" * 64,
            "size_bytes": 100,
            "media_type": "application/json",
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
                "artifact_id": mount.artifact_id,
                "mount_path": mount.mount_path,
                "content_digest": f"sha256:{mount.expected_content_sha256}",
                "localization_receipt_digest": f"sha256:{receipt}",
                "sub_path": mount.sub_path,
                "expected_manifest_sha256": mount.expected_manifest_sha256,
                "readiness_receipt_sha256": receipt,
                "authorization_receipt_sha256": mount.authorization_receipt_sha256,
            }
            for mount in invocation.runtime_mounts
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


def _compile(module, profile, request, *, model_id: str, models_module):
    kwargs: dict[str, object] = {"operation_id": f"adapter-contract-{model_id}"}
    if "variant_id" in inspect.signature(module.compile_run).parameters:
        kwargs["variant_id"] = module.VARIANT_ID
        kwargs["access_context"] = models_module.ArtifactAccessContext(
            profile="public", receipt_digest=None, tenant_id="adapter-contract-tenant"
        )
        kwargs["input_artifacts"] = (
            models_module.ScientificInputArtifact(
                logical_artifact_id="model-input",
                semantic_type="request/v1",
                artifact_id=UUID("00000000-0000-4000-8000-000000000001"),
                digest="sha256:" + RAW_SHA256,
                size_bytes=100,
                media_type="application/json",
                compression=None,
            ),
        )
    return module.compile_run(profile, request, **kwargs)


def _validate_profile_and_mounts(model_id, profile, plan, failures: list[str]) -> None:
    identity = profile.get("execution_identity")
    repository = (
        identity.get("runtime_image_repository")
        if isinstance(identity, dict)
        else None
    )
    if repository != EXPECTED_REPOSITORIES[model_id]:
        failures.append(
            f"{model_id} runtime repository {repository!r} is not "
            f"{EXPECTED_REPOSITORIES[model_id]!r}"
        )
    requirements = profile.get("artifact_requirements")
    artifacts = (
        {
            item["artifact_id"]: item
            for item in requirements
            if isinstance(item, dict)
            and isinstance(item.get("artifact_id"), str)
        }
        if isinstance(requirements, list)
        else {}
    )
    mounts = {
        mount.artifact_id: mount
        for invocation in plan.invocations
        for mount in getattr(invocation, "runtime_mounts", ())
    }
    for artifact_id, (digest, mount_path) in EXPECTED_ARTIFACTS[model_id].items():
        artifact = artifacts.get(artifact_id)
        if artifact is None or artifact.get("content_digest_sha256") != digest:
            failures.append(
                f"{model_id} profile lacks exact artifact identity {artifact_id}"
            )
        mount = mounts.get(artifact_id)
        if mount is None:
            failures.append(f"{model_id} plan lacks runtime mount {artifact_id}")
            continue
        if mount.mount_path != mount_path:
            failures.append(
                f"{model_id} mount {artifact_id} path {mount.mount_path!r} is not {mount_path!r}"
            )
        if getattr(mount, "expected_content_sha256", None) != digest:
            failures.append(f"{model_id} mount {artifact_id} lacks exact content digest")
        if getattr(mount, "authorization_receipt_sha256", None) is not None:
            failures.append(f"{model_id} retained request-time receipt gating")
    if model_id == "protenix-v2":
        artifact = artifacts.get("protenix-v2", {})
        if artifact.get("localization_manifest_sha256") != PROTENIX_MANIFEST_SHA256:
            failures.append("Protenix profile lacks exact localization manifest digest")
        mount = mounts.get("protenix-v2")
        if mount is not None and getattr(
            mount, "artifact_manifest_sha256", None
        ) != PROTENIX_MANIFEST_SHA256:
            failures.append("Protenix mount lacks exact localization manifest digest")


def _validate_deployment_caches(worktree: Path, failures: list[str]) -> dict[str, object]:
    path = (
        worktree
        / "k8s-inference/catalog/runtime/contracts/scientific-execution-targets.json"
    )
    try:
        bindings = json.loads(path.read_text(encoding="utf-8"))["bindings"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        failures.append(f"runtime deployment cache contract is unavailable: {exc}")
        return {}
    evidence: dict[str, object] = {}
    for (model_id, stage_id), expected in EXPECTED_DEPLOYMENT_CACHES.items():
        matches = [
            item
            for item in bindings
            if isinstance(item, dict)
            and item.get("model_id") == model_id
            and item.get("stage_id") == stage_id
        ]
        label = f"{model_id}/{stage_id}"
        if len(matches) != 1:
            failures.append(f"{label} requires one deployment cache binding")
            evidence[label] = {"valid": False}
            continue
        binding = matches[0]
        mounts = binding.get("mounts")
        caches = (
            [
                item
                for item in mounts
                if isinstance(item, dict) and item.get("kind") == "cache"
            ]
            if isinstance(mounts, list)
            else []
        )
        mount = caches[0] if len(caches) == 1 else None
        namespace = binding.get("execution_namespace")
        mount_valid = bool(
            mount
            and mount.get("mount_path") == expected["mount_path"]
            and mount.get("operator_owned") is True
            and mount.get("read_only") is False
            and isinstance(mount.get("claim_name"), str)
            and mount.get("claim_name")
            and mount.get("claim_namespace") == namespace
            and mount.get("artifact_id") is None
            and mount.get("host_path") is None
            and mount.get("sub_path") is None
        )
        environment = binding.get("environment")
        environment_valid = isinstance(environment, dict) and all(
            environment.get(key) == value
            for key, value in expected["environment"].items()
        )
        if not mount_valid:
            failures.append(f"{label} lacks exact persistent nonroot cache mount")
        if not environment_valid:
            failures.append(f"{label} lacks exact compiler-cache environment")
        evidence[label] = {
            "valid": mount_valid and environment_valid,
            "mount": mount,
            "environment": environment,
        }
    esm_caches = [
        item
        for item in bindings
        if isinstance(item, dict)
        and item.get("model_id") in {"esmfold2", "esmfold2-fast"}
        and isinstance(item.get("mounts"), list)
        and any(
            isinstance(mount, dict) and mount.get("kind") == "cache"
            for mount in item["mounts"]
        )
    ]
    if esm_caches:
        failures.append("ESM declares an unproven deployment compiler cache")
    evidence["esm_compiler_cache"] = "not-declared" if not esm_caches else "invalid"
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter-worktree", type=Path, required=True)
    args = parser.parse_args()
    worktree = args.adapter_worktree.resolve()
    control_plane = worktree / "k8s-inference/components/control-plane/src"
    profile_path = (
        worktree
        / "k8s-inference/catalog/runtime/contracts/scientific-workload-profiles.json"
    )
    if not control_plane.is_dir() or not profile_path.is_file():
        raise SystemExit(
            "adapter-worktree does not contain the runtime control-plane/profile set"
        )

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
    current_main = _git(task_repo, "rev-parse", "origin/main").stdout.strip()
    based_on_current_main = (
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

    sys.path.insert(0, str(control_plane))
    sys.path.insert(0, str(ROOT))
    from fs2_serve.scientific_batch import models as runtime_models
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
    profiles = json.loads(profile_path.read_text(encoding="utf-8"))["profiles"]
    actual_profiles = {model_id: _profile(profiles, model_id) for model_id in modules}
    failures: list[str] = []
    if dirty:
        failures.append("adapter worktree is dirty; evidence needs a concrete commit")
    if remote_revision != revision:
        failures.append(
            "adapter commit is not the exact clean pushed branch head "
            f"(local={revision}, remote={remote_revision or 'unavailable'})"
        )
    if not based_on_current_main:
        failures.append(
            "adapter commit is not based on current image-task origin/main "
            f"{current_main}"
        )

    plans: dict[str, object] = {}
    for model_id, module in modules.items():
        try:
            plans[model_id] = _compile(
                module,
                actual_profiles[model_id],
                _request(actual_profiles[model_id], parameters[model_id]),
                model_id=model_id,
                models_module=runtime_models,
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
        "adapter_branch": branch,
        "adapter_remote_revision": remote_revision or None,
        "adapter_is_exact_pushed_head": remote_revision == revision,
        "required_main_revision": current_main,
        "adapter_based_on_required_main": based_on_current_main,
        "deployment_caches": _validate_deployment_caches(worktree, failures),
        "models": {},
    }
    with tempfile.TemporaryDirectory() as temporary:
        marker_root = Path(temporary)
        for model_id, plan in plans.items():
            validated_stages: list[str] = []
            for invocation in plan.invocations:
                try:
                    parsed_args = _execute_parser(wrappers[model_id], invocation)
                    if invocation.runtime_artifacts:
                        _execute_runtime_marker(
                            wrappers[model_id],
                            invocation,
                            parsed_args,
                            model_id=model_id,
                            variant_id=plan.variant_id,
                            destination=marker_root,
                        )
                        validated_stages.append(invocation.stage_id)
                except BaseException as exc:  # argparse and wrappers raise SystemExit
                    failures.append(
                        f"{model_id} {invocation.stage_id} actual argv/marker failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            _validate_profile_and_mounts(
                model_id, actual_profiles[model_id], plan, failures
            )
            evidence["models"][model_id] = {
                "argv": [list(item.argv) for item in plan.invocations],
                "validated_runtime_localization_marker_stages": validated_stages,
                "runtime_mounts": [
                    {
                        "artifact_id": mount.artifact_id,
                        "mount_path": mount.mount_path,
                        "sub_path": getattr(mount, "sub_path", None),
                        "expected_content_sha256": getattr(
                            mount, "expected_content_sha256", None
                        ),
                        "artifact_manifest_sha256": getattr(
                            mount, "artifact_manifest_sha256", None
                        ),
                    }
                    for invocation in plan.invocations
                    for mount in getattr(invocation, "runtime_mounts", ())
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
