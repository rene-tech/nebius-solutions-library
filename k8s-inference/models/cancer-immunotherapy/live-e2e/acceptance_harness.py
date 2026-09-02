#!/usr/bin/env python3
"""Fail-closed preparation and evidence checks for cancer-immunotherapy acceptance.

The preparation commands are deliberately incapable of submitting work or changing
the cluster.  A later reviewed change may add an execution driver after every gate
reported by ``preflight`` is backed by an integrated contract.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PLAN_PATH = HERE / "acceptance-plan.json"
SCHEMA_PATH = HERE / "acceptance-plan.schema.json"
QUALIFICATION_PATH = ROOT / "models/cancer-immunotherapy/model-source-qualification.json"
PROFILE_PATH = ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
CANDIDATE_PATH = ROOT / "catalog/runtime/contracts/scientific-source-candidate-receipts.json"
ADMIN_FIXTURE_PATH = ROOT / "components/admin-console/contracts/scientific-admin-fixture-v1.json"

DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
IMAGE_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
GIT_SHA = re.compile(r"^[a-f0-9]{40}$")
READ_ONLY_KUBECTL_VERBS = frozenset({"get", "version", "api-resources", "api-versions"})


class HarnessError(RuntimeError):
    """A deterministic validation or preparation error."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessError(f"cannot read JSON contract {path}: {error}") from error


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_plan(plan: Mapping[str, Any]) -> None:
    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(plan),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        formatted = "; ".join(
            f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
            for error in errors
        )
        raise HarnessError(f"acceptance plan does not match its schema: {formatted}")

    requested = plan["requested_models"]
    model_ids = [model["model_id"] for model in plan["models"]]
    if requested != sorted(requested) or model_ids != sorted(model_ids):
        raise HarnessError("requested_models and models must use canonical model_id order")
    if set(requested) != set(model_ids):
        raise HarnessError("the model matrix must cover each requested model exactly once")
    if len(model_ids) != len(set(model_ids)):
        raise HarnessError("model matrix contains duplicate model IDs")
    scenario_ids = [scenario["id"] for scenario in plan["scenarios"]]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise HarnessError("acceptance scenarios must have unique IDs")
    for model in plan["models"]:
        rule_ids = [rule["id"] for rule in model["rules"]]
        if len(rule_ids) != len(set(rule_ids)):
            raise HarnessError(f"{model['model_id']} has duplicate validator rule IDs")
    levels = plan["timing"]["levels"]
    policy_levels = [item["level"] for item in plan["timing"]["level_policies"]]
    if policy_levels != levels:
        raise HarnessError("timing level policies must cover every level in canonical order")


def json_pointer(value: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise HarnessError(f"invalid JSON pointer: {pointer}")
    current = value
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise HarnessError(f"JSON pointer is absent: {pointer}")
        current = current[part]
    return current


def nested(value: Mapping[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _git_base_is_ancestor(base: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, "HEAD"],
        cwd=ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def preflight(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve all immutable integration gates without touching a live system."""

    validate_plan(plan)
    qualification = load_json(QUALIFICATION_PATH)
    profiles_contract = load_json(PROFILE_PATH)
    candidates_contract = load_json(CANDIDATE_PATH)
    admin_fixture = load_json(ADMIN_FIXTURE_PATH)
    blockers: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []

    if plan["semantic_policy"]["current_threshold_state"] != "reviewed-executable":
        blockers.append(
            {
                "gate": "semantic-validator",
                "code": "numeric_threshold_contracts_missing",
                "detail": plan["semantic_policy"]["current_threshold_state"],
            }
        )

    base = plan["base"]["git_commit"]
    if not _git_base_is_ancestor(base):
        blockers.append(
            {"gate": "reviewed-base", "code": "base_not_ancestor", "detail": base}
        )

    for contract in plan["base"]["contracts"]:
        path = ROOT / contract["path"]
        actual = file_sha256(path) if path.is_file() else None
        if actual != contract["sha256"]:
            blockers.append(
                {
                    "gate": "reviewed-contracts",
                    "code": "contract_digest_changed",
                    "detail": contract["path"],
                }
            )

    qualified_models = qualification.get("models", {})
    profiles = {
        profile.get("model_id"): profile
        for profile in profiles_contract.get("profiles", [])
        if isinstance(profile, Mapping) and isinstance(profile.get("model_id"), str)
    }
    candidates = {
        receipt.get("model_id"): receipt
        for receipt in candidates_contract.get("receipts", [])
        if isinstance(receipt, Mapping) and isinstance(receipt.get("model_id"), str)
    }

    if not profiles:
        blockers.extend(
            [
                {
                    "gate": "controller-integration",
                    "code": "production_batch_consumer_missing",
                    "detail": "PostgreSQL, Kubernetes/Kueue, runner and Helm wiring are not integrated",
                },
                {
                    "gate": "artifact-service",
                    "code": "scientific_artifact_commit_service_missing",
                    "detail": "no production content-addressed result commit/retrieval service is integrated",
                },
                {
                    "gate": "scheduling-policy",
                    "code": "cancer_queue_policy_missing",
                    "detail": (
                        "no exact tenant/model queue, fair-share, borrow, reclaim and "
                        "preemption policy is frozen"
                    ),
                },
            ]
        )

    for model in plan["models"]:
        model_id = model["model_id"]
        source = qualified_models.get(model_id)
        if not isinstance(source, Mapping):
            blockers.append(
                {"gate": "source-qualification", "code": "source_missing", "detail": model_id}
            )
            continue
        revision = nested(source, "code.revision")
        if revision != model["source_revision"]:
            blockers.append(
                {
                    "gate": "source-qualification",
                    "code": "source_revision_mismatch",
                    "detail": model_id,
                }
            )
        pointer_value = json_pointer(qualification, model["source_validator_pointer"])
        if not isinstance(pointer_value, Mapping) or not pointer_value.get("success_criteria"):
            blockers.append(
                {
                    "gate": "semantic-source-oracle",
                    "code": "source_oracle_missing",
                    "detail": model_id,
                }
            )

        candidate_id = "rfdiffusion-upstream" if model_id == "rfdiffusion" else model_id
        candidate = candidates.get(candidate_id)
        if isinstance(candidate, Mapping):
            candidate_revision = nested(candidate, "source.revision")
            if candidate_revision != model["source_revision"]:
                observations.append(
                    {
                        "gate": "candidate-source-receipt",
                        "code": "candidate_not_execution_authority",
                        "detail": f"{model_id}:{candidate_revision}",
                    }
                )
            if candidate.get("qualification_state") != "qualified":
                blockers.append(
                    {
                        "gate": "candidate-source-receipt",
                        "code": "candidate_unqualified",
                        "detail": model_id,
                    }
                )

        profile = profiles.get(model_id)
        if not isinstance(profile, Mapping):
            blockers.append(
                {"gate": "workload-profile", "code": "profile_missing", "detail": model_id}
            )
            continue
        for field in model["required_profile_fields"]:
            if nested(profile, field) is None:
                blockers.append(
                    {
                        "gate": "workload-profile",
                        "code": "profile_field_missing",
                        "detail": f"{model_id}:{field}",
                    }
                )
        if profile.get("state") != "qualified" or profile.get("route_exposed") is not True:
            blockers.append(
                {"gate": "workload-profile", "code": "profile_not_admitted", "detail": model_id}
            )
        if nested(profile, "source.revision") != model["source_revision"]:
            blockers.append(
                {
                    "gate": "runtime-identity",
                    "code": "profile_source_revision_mismatch",
                    "detail": model_id,
                }
            )
        if IMAGE_DIGEST.fullmatch(str(nested(profile, "runtime.image.digest") or "")) is None:
            blockers.append(
                {"gate": "runtime-identity", "code": "image_digest_missing", "detail": model_id}
            )
        if nested(profile, "semantic_validation.validator_id") != model["validator_id"]:
            blockers.append(
                {"gate": "semantic-validator", "code": "validator_binding_missing", "detail": model_id}
            )

    if admin_fixture.get("status") != "production-backend-integrated":
        blockers.append(
            {
                "gate": "admin-surface",
                "code": "admin_scientific_routes_fixture_only",
                "detail": str(admin_fixture.get("status", "missing")),
            }
        )

    source_files = list((ROOT / "components/control-plane/src/fs2_serve").glob("*.py"))
    production_source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    required_routes = (
        "/admin/api/v1/scientific-runs",
        "/admin/api/v1/scientific-models",
        ":submit",
    )
    for route in required_routes:
        if route not in production_source:
            blockers.append(
                {"gate": "production-surfaces", "code": "route_missing", "detail": route}
            )

    telemetry_source = (ROOT / "components/control-plane/src/fs2_serve/telemetry.py").read_text(
        encoding="utf-8"
    )
    if "not measured utilization" in telemetry_source or "estimated_gpu_seconds" in telemetry_source:
        blockers.append(
            {
                "gate": "telemetry",
                "code": "exact_gpu_lifecycle_not_integrated",
                "detail": "generic telemetry remains estimate-only",
            }
        )

    controller_source = (
        ROOT / "components/control-plane/src/fs2_serve/scientific_batch/models.py"
    ).read_text(encoding="utf-8")
    if "class WorkloadResource" in controller_source and "acceptance-run" not in controller_source:
        blockers.append(
            {
                "gate": "cleanup",
                "code": "acceptance_ownership_labels_not_propagated",
                "detail": "production Job writer must bind canonical labels and exact UID ownership",
            }
        )

    # One occurrence per exact tuple keeps reports stable while retaining all causes.
    blockers = sorted(
        {canonical_json(item): item for item in blockers}.values(),
        key=lambda item: (item["gate"], item["code"], item["detail"]),
    )
    observations = sorted(
        {canonical_json(item): item for item in observations}.values(),
        key=lambda item: (item["gate"], item["code"], item["detail"]),
    )
    return {
        "schema": "fs2-serve.nebius.ai/cancer-immunotherapy-acceptance-preflight/v1",
        "mode": "preparation-only",
        "ready_for_execution": not blockers,
        "requested_model_count": len(plan["requested_models"]),
        "admitted_profile_count": sum(
            1
            for model_id in plan["requested_models"]
            if isinstance(profiles.get(model_id), Mapping)
            and profiles[model_id].get("state") == "qualified"
            and profiles[model_id].get("route_exposed") is True
        ),
        "blockers": blockers,
        "observations": observations,
    }


def render_cases(plan: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    validate_plan(plan)
    if len(run_id) > 32 or DNS_LABEL.fullmatch(run_id) is None:
        raise HarnessError("run_id must be a DNS label of at most 32 characters")
    cases: list[dict[str, Any]] = []
    for model in plan["models"]:
        for ordinal in (1, 2):
            case_id = f"{model['model_id']}-{ordinal}"
            operation_id = str(uuid5(NAMESPACE_URL, f"fs2-live-e2e:{run_id}:{case_id}"))
            attempt_id = f"{run_id}-{model['seed']}-{ordinal}"
            workload_id = f"{run_id}-{model['seed']}"
            labels = {
                "app.kubernetes.io/managed-by": "fs2-live-acceptance",
                "fs2.nebius.ai/acceptance-run": run_id,
                "fs2.nebius.ai/acceptance-scenario": "semantic-model-lanes",
                "fs2.nebius.ai/model-id": model["model_id"],
                "fs2.nebius.ai/workload-id": workload_id,
                "fs2.nebius.ai/attempt-id": attempt_id,
                "fs2.nebius.ai/tenant-id": "acceptance-a",
                "fs2.nebius.ai/service-class": "customer-batch",
                "fs2.nebius.ai/local-queue": "unresolved",
            }
            cases.append(
                {
                    "case_id": case_id,
                    "operation_id": operation_id,
                    "attempt_id": attempt_id,
                    "model_id": model["model_id"],
                    "source_revision": model["source_revision"],
                    "validator_id": model["validator_id"],
                    "seed": model["seed"] + ordinal - 1,
                    "service_class": "customer-batch",
                    "labels": labels,
                    "annotations": {
                        "fs2.nebius.ai/acceptance-task": "fs2-cancer-immunotherapy-live-e2e-acceptance-r20260902",
                        "fs2.nebius.ai/operation-id": operation_id,
                    },
                    "submission_state": "blocked-until-preflight-ready",
                }
            )
    return {
        "schema": "fs2-serve.nebius.ai/cancer-immunotherapy-acceptance-cases/v1",
        "run_id": run_id,
        "mode": "preparation-only",
        "mutations_permitted": False,
        "cases": cases,
        "sha256": hashlib.sha256(canonical_json(cases)).hexdigest(),
    }


def _matching_files(root: Path, patterns: Iterable[str]) -> list[Path]:
    matches: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            current = root
            unsafe = False
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    unsafe = True
                    break
            if not unsafe and path.is_file():
                matches.add(path)
    return sorted(matches)


def _structure_counts(path: Path) -> tuple[int, int]:
    atoms = 0
    chains: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith(("ATOM  ", "HETATM", "ATOM ", "HETATM ")):
                continue
            atoms += 1
            if path.suffix.lower() == ".pdb":
                chain = line[21:22].strip() or "_"
            else:
                fields = line.split()
                chain = fields[6] if len(fields) > 6 else "_"
            chains.add(chain)
            if path.suffix.lower() == ".pdb" and len(line) >= 54:
                try:
                    coordinates = tuple(float(line[start:end]) for start, end in ((30, 38), (38, 46), (46, 54)))
                except ValueError as error:
                    raise HarnessError(f"non-numeric PDB coordinate in {path}") from error
                if not all(math.isfinite(value) for value in coordinates):
                    raise HarnessError(f"non-finite PDB coordinate in {path}")
    return atoms, len(chains)


def _finite_number_count(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(math.isfinite(float(value)))
    if isinstance(value, Mapping):
        return sum(_finite_number_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_finite_number_count(item) for item in value)
    return 0


def validate_artifacts(plan: Mapping[str, Any], model_id: str, result_dir: Path) -> dict[str, Any]:
    validate_plan(plan)
    model = next((item for item in plan["models"] if item["model_id"] == model_id), None)
    if model is None:
        raise HarnessError(f"model is outside the requested acceptance matrix: {model_id}")
    root = result_dir.resolve(strict=True)
    if not root.is_dir():
        raise HarnessError("result_dir must be a directory")

    receipt_path = root / "semantic-validation-receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise HarnessError("semantic validation receipt must be a regular non-symlink file")
    receipt = load_json(receipt_path)
    required_receipt_fields = {
        "schema",
        "model_id",
        "source_revision",
        "runtime_image_digest",
        "validator_id",
        "status",
        "input_manifest_sha256",
        "output_manifest_sha256",
        "checks",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != required_receipt_fields:
        raise HarnessError("semantic validation receipt fields are not exact")
    if receipt["schema"] != "fs2-serve.nebius.ai/scientific-semantic-validation-receipt/v1":
        raise HarnessError("semantic validation receipt schema is unsupported")
    if receipt["model_id"] != model_id or receipt["source_revision"] != model["source_revision"]:
        raise HarnessError("semantic validation receipt identifies another model source")
    if receipt["validator_id"] != model["validator_id"] or receipt["status"] != "passed":
        raise HarnessError("authoritative semantic validator did not pass")
    if IMAGE_DIGEST.fullmatch(str(receipt["runtime_image_digest"])) is None:
        raise HarnessError("semantic validation receipt lacks an immutable image digest")
    for key in ("input_manifest_sha256", "output_manifest_sha256"):
        if DIGEST.fullmatch(str(receipt[key])) is None:
            raise HarnessError(f"semantic validation receipt has invalid {key}")
    checks = receipt["checks"]
    if not isinstance(checks, list):
        raise HarnessError("semantic validation receipt checks must be an array")
    passed_ids = {
        item.get("id")
        for item in checks
        if isinstance(item, Mapping)
        and set(item) == {"id", "status", "evidence_sha256"}
        and item.get("status") == "passed"
        and DIGEST.fullmatch(str(item.get("evidence_sha256", ""))) is not None
    }

    required_semantic_ids = {item["id"] for item in model["oracle_assertions"]}
    missing_semantic_ids = sorted(required_semantic_ids - passed_ids)
    results: list[dict[str, Any]] = []
    for rule in model["rules"]:
        paths = _matching_files(root, rule["patterns"])
        valid: list[str] = []
        for path in paths:
            if path.stat().st_size < rule.get("min_bytes", 1):
                continue
            if rule["kind"] == "protein-structure":
                atoms, chains = _structure_counts(path)
                if atoms < rule.get("min_atom_records", 1) or chains < rule.get("min_polymer_chains", 1):
                    continue
            elif rule["kind"] == "tabular-result":
                with path.open("r", encoding="utf-8", newline="") as handle:
                    rows = list(csv.reader(handle))
                if max(0, len(rows) - 1) < rule.get("min_rows", 1):
                    continue
            elif rule["kind"] == "json-numerics":
                if _finite_number_count(load_json(path)) < rule.get("min_finite_numbers", 1):
                    continue
            valid.append(str(path.relative_to(root)))
        passed = len(valid) >= rule["minimum"] and rule["id"] in passed_ids
        results.append({"rule_id": rule["id"], "passed": passed, "files": valid})

    passed = all(item["passed"] for item in results) and not missing_semantic_ids
    return {
        "schema": "fs2-serve.nebius.ai/cancer-immunotherapy-artifact-check/v1",
        "model_id": model_id,
        "passed": passed,
        "rules": results,
        "missing_authoritative_oracle_checks": missing_semantic_ids,
    }


def _kubectl_json(context: str, kubeconfig: Path, arguments: list[str]) -> Any:
    if not arguments or arguments[0] not in READ_ONLY_KUBECTL_VERBS:
        raise HarnessError("inventory attempted a non-read-only kubectl verb")
    command = [
        "kubectl",
        f"--kubeconfig={kubeconfig}",
        f"--context={context}",
        *arguments,
        "-o",
        "json",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        raise HarnessError(f"kubectl read failed for {' '.join(arguments)}: {completed.stderr.strip()}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise HarnessError("kubectl returned invalid JSON") from error


def _selected_labels(labels: Mapping[str, Any]) -> dict[str, str]:
    prefixes = (
        "accelerator.fs2.nebius/",
        "capacity.fs2.nebius/",
        "workload.fs2.nebius/",
        "node.kubernetes.io/instance-type",
        "topology.kubernetes.io/",
        "kueue.x-k8s.io/",
        "fs2-serve.nebius.ai/",
        "fs2.nebius.ai/",
        "app.kubernetes.io/",
    )
    return {key: str(value) for key, value in sorted(labels.items()) if key.startswith(prefixes)}


def _container_inventory(spec: Mapping[str, Any], status: Mapping[str, Any]) -> list[dict[str, Any]]:
    status_by_name = {
        item.get("name"): item
        for item in status.get("containerStatuses", [])
        if isinstance(item, Mapping)
    }
    containers: list[dict[str, Any]] = []
    for item in spec.get("containers", []):
        if not isinstance(item, Mapping):
            continue
        resources = item.get("resources", {}) if isinstance(item.get("resources"), Mapping) else {}
        requests = resources.get("requests", {}) if isinstance(resources.get("requests"), Mapping) else {}
        limits = resources.get("limits", {}) if isinstance(resources.get("limits"), Mapping) else {}
        container_status = status_by_name.get(item.get("name"), {})
        containers.append(
            {
                "name": item.get("name"),
                "image": item.get("image"),
                "image_id": container_status.get("imageID"),
                "gpu_request": requests.get("nvidia.com/gpu"),
                "gpu_limit": limits.get("nvidia.com/gpu"),
                "restart_count": container_status.get("restartCount", 0),
            }
        )
    return containers


def inventory_cluster(plan: Mapping[str, Any], kubeconfig: Path) -> dict[str, Any]:
    """Collect a secret-free live inventory using only kubectl get calls."""

    validate_plan(plan)
    context = plan["target"]["context"]
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise HarnessError("kubeconfig must be an existing absolute file")
    nodes = _kubectl_json(context, kubeconfig, ["get", "nodes"])
    objects = _kubectl_json(
        context,
        kubeconfig,
        ["get", "deployments,statefulsets,daemonsets,jobs,pods", "--all-namespaces"],
    )
    queues: dict[str, Any] = {}
    for name, resource, namespaced in (
        ("resourceflavors", "resourceflavors.kueue.x-k8s.io", False),
        ("cohorts", "cohorts.kueue.x-k8s.io", False),
        ("clusterqueues", "clusterqueues.kueue.x-k8s.io", False),
        ("localqueues", "localqueues.kueue.x-k8s.io", True),
        ("workloads", "workloads.kueue.x-k8s.io", True),
        ("workloadpriorityclasses", "workloadpriorityclasses.kueue.x-k8s.io", False),
    ):
        args = ["get", resource]
        if namespaced:
            args.append("--all-namespaces")
        try:
            queues[name] = _kubectl_json(context, kubeconfig, args).get("items", [])
        except HarnessError as error:
            queues[name] = {"unavailable": str(error)}

    node_rows = []
    for node in nodes.get("items", []):
        metadata = node.get("metadata", {})
        status = node.get("status", {})
        capacity = status.get("capacity", {}) if isinstance(status.get("capacity"), Mapping) else {}
        allocatable = status.get("allocatable", {}) if isinstance(status.get("allocatable"), Mapping) else {}
        node_rows.append(
            {
                "name": metadata.get("name"),
                "uid": metadata.get("uid"),
                "labels": _selected_labels(metadata.get("labels", {})),
                "capacity": {
                    "cpu": capacity.get("cpu"),
                    "memory": capacity.get("memory"),
                    "nvidia.com/gpu": capacity.get("nvidia.com/gpu"),
                },
                "allocatable": {
                    "cpu": allocatable.get("cpu"),
                    "memory": allocatable.get("memory"),
                    "nvidia.com/gpu": allocatable.get("nvidia.com/gpu"),
                },
                "ready": any(
                    condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in status.get("conditions", [])
                    if isinstance(condition, Mapping)
                ),
            }
        )

    workload_rows = []
    for item in objects.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        pod_spec = spec if item.get("kind") == "Pod" else nested(spec, "template.spec") or {}
        pod_status = status if item.get("kind") == "Pod" else {}
        workload_rows.append(
            {
                "kind": item.get("kind"),
                "namespace": metadata.get("namespace"),
                "name": metadata.get("name"),
                "uid": metadata.get("uid"),
                "creation_timestamp": metadata.get("creationTimestamp"),
                "labels": _selected_labels(metadata.get("labels", {})),
                "owners": [
                    {"kind": owner.get("kind"), "name": owner.get("name"), "uid": owner.get("uid")}
                    for owner in metadata.get("ownerReferences", [])
                    if isinstance(owner, Mapping)
                ],
                "node_name": pod_spec.get("nodeName"),
                "phase": status.get("phase"),
                "images": _container_inventory(pod_spec, pod_status),
            }
        )

    return {
        "schema": "fs2-serve.nebius.ai/cancer-immunotherapy-live-inventory/v1",
        "collection_mode": "kubectl-get-only",
        "target": plan["target"],
        "nodes": sorted(node_rows, key=lambda row: str(row["name"])),
        "workloads": sorted(
            workload_rows,
            key=lambda row: (str(row["namespace"]), str(row["kind"]), str(row["name"])),
        ),
        "kueue": queues,
    }


def _write_json(value: Any, output: str | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    path = Path(output)
    path.write_text(rendered, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=PLAN_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="validate the checked-in deterministic plan")
    subparsers.add_parser("preflight", help="resolve local reviewed-contract gates; exit 2 when blocked")

    render = subparsers.add_parser("render", help="render deterministic, non-submittable case identities")
    render.add_argument("--run-id", required=True)
    render.add_argument("--output")

    artifacts = subparsers.add_parser("validate-artifacts", help="validate retained output and semantic receipt")
    artifacts.add_argument("--model-id", required=True)
    artifacts.add_argument("--result-dir", type=Path, required=True)

    inventory = subparsers.add_parser("inventory", help="capture secret-free cluster state with kubectl get only")
    inventory.add_argument("--kubeconfig", type=Path, required=True)
    inventory.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = load_json(args.plan)
        if args.command == "validate":
            validate_plan(plan)
            _write_json({"valid": True, "mode": plan["mode"]}, None)
            return 0
        if args.command == "preflight":
            report = preflight(plan)
            _write_json(report, None)
            return 0 if report["ready_for_execution"] else 2
        if args.command == "render":
            _write_json(render_cases(plan, args.run_id), args.output)
            return 0
        if args.command == "validate-artifacts":
            report = validate_artifacts(plan, args.model_id, args.result_dir)
            _write_json(report, None)
            return 0 if report["passed"] else 2
        if args.command == "inventory":
            _write_json(inventory_cluster(plan, args.kubeconfig), args.output)
            return 0
        raise HarnessError("unknown command")
    except HarnessError as error:
        sys.stderr.write(f"acceptance harness: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
