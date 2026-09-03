#!/usr/bin/env python3
"""Render task-owned Kueue contention Jobs from an applied scheduling contract.

The renderer performs no cluster access and never creates queue policy. Its
output is JSON accepted by kubectl after an operator reviews the exact objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

SCHEMA = "fs2-serve.nebius.ai/kueue-scheduling/v1"
IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# One place defines the victim's grace window, so the manifest and the
# documented graceful-termination expectation cannot drift apart.
TERMINATION_GRACE_SECONDS = 30
DNS_LABEL = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
# A qualified resource name is a prefix of at most 253 characters, a slash, and
# a name of at most 63. The lookahead bounds each half; a total-length bound
# alone would accept a 300-character prefix the API server rejects.
EXTENDED_RESOURCE_RE = re.compile(
    rf"^(?=[^/]{{1,253}}/[^/]{{1,63}}$){DNS_LABEL}(?:\.{DNS_LABEL})*"
    r"/[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?$"
)


class ContractError(ValueError):
    """The supplied policy cannot deterministically render this scenario."""


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ContractError(f"{label} must be an object")
    return value


def _load(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    """Return the applied policy and the digest of its exact raw bytes.

    The digest must come from the bytes as applied, not from a Python
    reserialization: Terraform's jsonencode escapes <, >, and & and preserves
    UTF-8, so a round-trip through json.dumps would hash different bytes.
    """

    try:
        raw = path.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise ContractError("scheduling contract exceeds 4 MiB")
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ContractError("scheduling contract is unavailable or invalid") from error
    revision = hashlib.sha256(raw).hexdigest()
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise ContractError("expected contract SHA-256 must be 64 lowercase hex characters")
    if revision != expected_sha256:
        raise ContractError(
            "scheduling contract bytes do not match the expected scheduling_contract_ref SHA-256"
        )
    contract = _object(value, "scheduling contract")
    if contract.get("schema") != SCHEMA:
        raise ContractError("scheduling contract schema is unsupported")
    return contract, revision


def _dns_label(value: str, label: str, *, maximum: int = 63) -> str:
    if not 1 <= len(value) <= maximum or DNS_LABEL_RE.fullmatch(value) is None:
        raise ContractError(f"{label} must be a DNS label of at most {maximum} characters")
    return value


def _label_value(value: str, label: str) -> str:
    if not 1 <= len(value) <= 63 or LABEL_VALUE_RE.fullmatch(value) is None:
        raise ContractError(f"{label} must be a Kubernetes label value of at most 63 characters")
    return value


class Policy:
    def __init__(self, contract: Mapping[str, Any], revision: str) -> None:
        self.contract = dict(contract)
        self.revision = revision
        self.namespace_bound_models = _object(
            contract.get("namespace_bound_models", {}), "namespace_bound_models"
        )
        self.model_eligible_pool_ids = _object(
            contract.get("model_eligible_pool_ids", {}), "model_eligible_pool_ids"
        )
        self.service_classes = _object(contract.get("service_classes"), "service_classes")
        self.local_queues = _object(contract.get("local_queues"), "local_queues")
        self.cluster_queues = _object(contract.get("cluster_queues"), "cluster_queues")
        self.priority_classes = _object(contract.get("workload_priority_classes"), "workload_priority_classes")
        self.routes = _object(contract.get("local_queue_routes"), "local_queue_routes")
        self.pools = _object(contract.get("pools"), "pools")

    def _route(self, *, default_queue: str, service_class: str, model_id: str, tenant_id: str) -> str:
        """Exact tenant+model+class, then wildcard-tenant model+class, then default."""

        exact: list[str] = []
        model_default: list[str] = []
        for queue_name, raw_route in self.routes.items():
            route = _object(raw_route, f"LocalQueue route {queue_name}")
            models = route.get("model_ids")
            tenants = route.get("tenant_ids")
            service_classes = route.get("service_classes")
            if (
                not isinstance(models, list)
                or not isinstance(tenants, list)
                or not isinstance(service_classes, list)
            ):
                raise ContractError(f"LocalQueue route {queue_name} is incomplete")
            if model_id not in models or service_class not in service_classes:
                continue
            if tenants and tenant_id in tenants:
                exact.append(queue_name)
            elif not tenants:
                model_default.append(queue_name)
        candidates = exact or model_default
        if len(candidates) > 1:
            raise ContractError("LocalQueue routes are ambiguous for this tenant and model")
        if candidates:
            return candidates[0]
        # Falling back to the service class default is only valid when that
        # lane is unrestricted. A tenant- or model-restricted lane must not
        # admit a caller the route does not name.
        fallback = _object(self.routes.get(default_queue), f"LocalQueue route {default_queue}")
        models = fallback.get("model_ids")
        tenants = fallback.get("tenant_ids")
        service_classes = fallback.get("service_classes")
        if (
            not isinstance(models, list)
            or not isinstance(tenants, list)
            or not isinstance(service_classes, list)
        ):
            raise ContractError(f"LocalQueue route {default_queue} is incomplete")
        if models and model_id not in models:
            raise ContractError(
                f"default LocalQueue {default_queue} is restricted to other models, so "
                f"{model_id} has no lane for {service_class}"
            )
        if tenants and tenant_id not in tenants:
            raise ContractError(
                f"default LocalQueue {default_queue} is restricted to other tenants, so "
                f"{tenant_id} has no lane for {service_class}"
            )
        if service_classes and service_class not in service_classes:
            raise ContractError(
                f"default LocalQueue {default_queue} does not declare service class {service_class}"
            )
        return default_queue

    def resolve(
        self,
        *,
        service_class: str,
        queue_override: str | None,
        model_id: str,
        tenant_id: str,
        pool_id: str | None,
    ) -> dict[str, Any]:
        policy = _object(self.service_classes.get(service_class), f"service class {service_class}")
        if policy.get("caller_selectable") is not True:
            raise ContractError(f"service class {service_class} is not caller-selectable")
        default_queue = policy.get("default_local_queue")
        if not isinstance(default_queue, str):
            raise ContractError(f"service class {service_class} has no default LocalQueue")
        resolved = self._route(
            default_queue=default_queue,
            service_class=service_class,
            model_id=model_id,
            tenant_id=tenant_id,
        )
        # An override may only name the lane the contract already resolves, so
        # a scenario can be explicit without bypassing the policy.
        queue_name = queue_override or resolved
        if queue_name != resolved:
            raise ContractError(
                f"LocalQueue {queue_name} is not the lane this policy resolves for "
                f"{service_class}/{tenant_id}/{model_id} ({resolved})"
            )
        queue = _object(self.local_queues.get(queue_name), f"LocalQueue {queue_name}")
        queue_metadata = _object(queue.get("metadata"), f"LocalQueue {queue_name} metadata")
        queue_spec = _object(queue.get("spec"), f"LocalQueue {queue_name} spec")
        namespace = queue_metadata.get("namespace")
        cluster_queue_name = queue_spec.get("clusterQueue")
        if not isinstance(namespace, str) or not isinstance(cluster_queue_name, str):
            raise ContractError(f"LocalQueue {queue_name} identity is incomplete")
        # A model whose assets exist in one namespace must never fall through to
        # another namespace, where its claim cannot be mounted.
        required_namespace = self.namespace_bound_models.get(model_id)
        if isinstance(required_namespace, str) and namespace != required_namespace:
            raise ContractError(
                f"model {model_id} is bound to namespace {required_namespace} but this policy "
                f"resolves {service_class}/{tenant_id} to namespace {namespace}"
            )
        cluster_queue = _object(
            self.cluster_queues.get(cluster_queue_name), f"ClusterQueue {cluster_queue_name}"
        )

        priority_name = policy.get("workload_priority_class")
        priority_value = policy.get("priority")
        priority = _object(self.priority_classes.get(priority_name), f"priority class {priority_name}")
        if (
            not isinstance(priority_name, str)
            or not isinstance(priority_value, int)
            or isinstance(priority_value, bool)
            or priority.get("value") != priority_value
        ):
            raise ContractError(f"service class {service_class} priority is inconsistent")

        preference = policy.get("pool_preference")
        if not isinstance(preference, list) or not preference or not all(isinstance(item, str) for item in preference):
            raise ContractError(f"service class {service_class} pool preference is incomplete")
        # A service class lists every deployed pool, so its head says nothing
        # about whether this model is qualified there. Intersect with the
        # authoritative eligibility and refuse an empty result.
        eligible = self.model_eligible_pool_ids.get(model_id)
        if not isinstance(eligible, list) or not eligible or not all(isinstance(item, str) for item in eligible):
            raise ContractError(
                f"model {model_id} has no eligible pools in this contract, so it has no lane here"
            )
        preference = [item for item in preference if item in eligible]
        if not preference:
            raise ContractError(
                f"model {model_id} is not qualified for any pool in service class {service_class}"
            )
        eligible_order = list(preference)
        selected_pool = pool_id or eligible_order[0]
        if selected_pool not in eligible_order:
            raise ContractError(
                f"pool {selected_pool} is outside the pools model {model_id} may use "
                f"in service class {service_class}"
            )
        pool = _object(self.pools.get(selected_pool), f"pool {selected_pool}")
        resource_name = pool.get("accelerator_resource_name")
        resource_flavor = pool.get("resource_flavor")
        if (
            not isinstance(resource_name, str)
            or EXTENDED_RESOURCE_RE.fullmatch(resource_name) is None
            or not isinstance(resource_flavor, str)
        ):
            raise ContractError(f"pool {selected_pool} accelerator mapping is invalid")

        pool_label_key = self.contract.get("pool_node_label_key")
        if not isinstance(pool_label_key, str) or not pool_label_key:
            raise ContractError("the contract does not publish a canonical pool label key")

        groups = _object(cluster_queue.get("spec"), f"ClusterQueue {cluster_queue_name} spec").get(
            "resourceGroups"
        )
        if not isinstance(groups, list):
            raise ContractError(f"ClusterQueue {cluster_queue_name} resource groups are absent")
        flavor_is_available = any(
            isinstance(group, dict)
            and resource_name in group.get("coveredResources", [])
            and any(
                isinstance(flavor, dict) and flavor.get("name") == resource_flavor
                for flavor in group.get("flavors", [])
            )
            for group in groups
        )
        if not flavor_is_available:
            raise ContractError(
                f"pool {selected_pool} is not available from ClusterQueue {cluster_queue_name}"
            )

        return {
            "service_class": service_class,
            "queue": queue_name,
            "namespace": namespace,
            "cluster_queue": cluster_queue_name,
            "priority_class": priority_name,
            "priority": priority_value,
            "preemption_mode": policy.get("preemption_mode"),
            "max_queue_seconds": policy.get("max_queue_seconds"),
            "max_execution_seconds": policy.get("max_execution_seconds"),
            "pool_id": selected_pool,
            # Every pool this model may use, in the queue's search order. The
            # Pod is constrained to exactly this set, so Kueue's flavor order
            # can only pick a compatible one.
            "eligible_pool_ids": eligible_order,
            "pool_node_label_key": pool_label_key,
            "resource_flavor": resource_flavor,
            "resource_name": resource_name,
        }


def _job(
    *,
    run_id: str,
    role: str,
    ordinal: int,
    image: str,
    model_id: str,
    tenant_id: str,
    decision: Mapping[str, Any],
    revision: str,
    parallelism: int,
    minimum_parallelism: int | None,
    hold_seconds: int,
    force_pool: bool,
) -> dict[str, Any]:
    identity = f"{run_id}:{role}:{ordinal}:{tenant_id}:{model_id}"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
    name = f"fs2-sa-{run_id}-{role}-{ordinal}-{suffix}"[:63].rstrip("-")
    operation_id = str(uuid5(NAMESPACE_URL, f"fs2-scheduling-acceptance:{identity}:operation"))
    workload_id = str(uuid5(NAMESPACE_URL, f"fs2-scheduling-acceptance:{identity}:workload"))
    attempt_id = str(uuid5(NAMESPACE_URL, f"fs2-scheduling-acceptance:{identity}:attempt:1"))
    labels = {
        "app.kubernetes.io/name": "fs2-scheduling-acceptance",
        "fs2.nebius.ai/acceptance-run": run_id,
        "fs2.nebius.ai/acceptance-role": role,
        "fs2.nebius.ai/operation-id": operation_id,
        "fs2.nebius.ai/workload-id": workload_id,
        "fs2.nebius.ai/attempt-id": attempt_id,
        "fs2.nebius.ai/model-id": model_id,
        "fs2.nebius.ai/tenant-id": tenant_id,
        "fs2.nebius.ai/service-class": str(decision["service_class"]),
        "fs2.nebius.ai/local-queue": str(decision["queue"]),
        "kueue.x-k8s.io/queue-name": str(decision["queue"]),
        "kueue.x-k8s.io/priority-class": str(decision["priority_class"]),
    }
    max_execution = decision.get("max_execution_seconds")
    if isinstance(max_execution, int):
        labels["kueue.x-k8s.io/max-exec-time-seconds"] = str(max_execution)
    annotations = {
        "fs2.nebius.ai/scheduling-contract-sha256": revision,
        "fs2.nebius.ai/cluster-queue": str(decision["cluster_queue"]),
        "fs2.nebius.ai/pool-preference-head": str(decision["pool_id"]),
        "fs2.nebius.ai/eligible-pool-ids": ",".join(
            str(item) for item in decision["eligible_pool_ids"]
        ),
        "fs2.nebius.ai/resource-flavor-preference": str(decision["resource_flavor"]),
        "fs2.nebius.ai/preemption-mode": str(decision["preemption_mode"]),
    }
    max_queue = decision.get("max_queue_seconds")
    if isinstance(max_queue, int):
        annotations["fs2.nebius.ai/max-queue-seconds"] = str(max_queue)
    if minimum_parallelism is not None:
        annotations["kueue.x-k8s.io/job-min-parallelism"] = str(minimum_parallelism)

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": TERMINATION_GRACE_SECONDS,
        "containers": [
            {
                "name": "holder",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-ec"],
                "args": [
                    "trap 'echo fs2-acceptance-graceful-termination; exit 0' TERM INT; "
                    f"echo fs2-acceptance-ready; end=$(( $(date +%s) + {hold_seconds} )); "
                    "while [ $(date +%s) -lt $end ]; do sleep 5; done"
                ],
                "resources": {
                    "requests": {str(decision["resource_name"]): "1", "cpu": "10m", "memory": "16Mi"},
                    "limits": {str(decision["resource_name"]): "1", "memory": "16Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "runAsNonRoot": True,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
            }
        ],
    }
    eligible_pool_ids = [str(item) for item in decision["eligible_pool_ids"]]
    pool_label_key = str(decision["pool_node_label_key"])
    # Required node affinity, always. Kueue picks a flavor from the queue's
    # order and does not read a custom annotation, so without this a model can
    # be admitted onto an accelerator it is not qualified for whenever the
    # queue's preferred flavor happens to be incompatible.
    pod_spec["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": pool_label_key,
                                "operator": "In",
                                "values": eligible_pool_ids,
                            }
                        ]
                    }
                ]
            }
        }
    }
    if force_pool:
        # An explicit operator choice narrows it to exactly one pool.
        pod_spec["nodeSelector"] = {pool_label_key: str(decision["pool_id"])}
    job_spec: dict[str, Any] = {
        "suspend": True,
        "backoffLimit": 0,
        "parallelism": parallelism,
        "completions": parallelism,
        "completionMode": "Indexed",
        "template": {"metadata": {"labels": labels}, "spec": pod_spec},
    }
    if isinstance(max_execution, int):
        job_spec["activeDeadlineSeconds"] = max_execution
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": decision["namespace"],
            "labels": labels,
            "annotations": annotations,
        },
        "spec": job_spec,
    }


def render(args: argparse.Namespace) -> dict[str, Any]:
    contract, revision = _load(args.contract, args.contract_sha256)
    policy = Policy(contract, revision)
    _dns_label(args.run_id, "run ID", maximum=20)
    _dns_label(args.model_id, "model ID")
    _label_value(args.tenant_a, "tenant A")
    _label_value(args.tenant_b, "tenant B")
    if IMAGE_RE.fullmatch(args.image) is None:
        raise ContractError("image must be pinned by sha256 digest")
    if not 1 <= args.parallelism <= 1024:
        raise ContractError("parallelism must be between 1 and 1024")
    if not 30 <= args.hold_seconds <= 86400:
        raise ContractError("hold seconds must be between 30 and 86400")
    if args.minimum_parallelism is not None and not 1 <= args.minimum_parallelism < args.parallelism:
        raise ContractError("minimum parallelism must be positive and lower than parallelism")

    def decision(service_class: str, queue: str | None, tenant: str) -> dict[str, Any]:
        return policy.resolve(
            service_class=service_class,
            queue_override=queue,
            model_id=args.model_id,
            tenant_id=tenant,
            pool_id=args.pool_id,
        )

    jobs: list[dict[str, Any]] = []
    if args.scenario == "victims":
        selected = decision(args.victim_service_class, args.queue_a, args.tenant_a)
        jobs.append(
            _job(
                run_id=args.run_id,
                role="victim",
                ordinal=1,
                image=args.image,
                model_id=args.model_id,
                tenant_id=args.tenant_a,
                decision=selected,
                revision=policy.revision,
                parallelism=args.parallelism,
                minimum_parallelism=None,
                hold_seconds=args.hold_seconds,
                force_pool=args.pool_id is not None,
            )
        )
    elif args.scenario == "preemptor":
        selected = decision(args.preemptor_service_class, args.queue_a, args.tenant_a)
        jobs.append(
            _job(
                run_id=args.run_id,
                role="preemptor",
                ordinal=1,
                image=args.image,
                model_id=args.model_id,
                tenant_id=args.tenant_a,
                decision=selected,
                revision=policy.revision,
                parallelism=1,
                minimum_parallelism=None,
                hold_seconds=args.hold_seconds,
                force_pool=args.pool_id is not None,
            )
        )
    elif args.scenario == "fairness":
        if args.queue_a is None or args.queue_b is None or args.queue_a == args.queue_b:
            raise ContractError("fairness requires two distinct explicit LocalQueues")
        for ordinal, (queue, tenant) in enumerate(((args.queue_a, args.tenant_a), (args.queue_b, args.tenant_b)), 1):
            selected = decision(args.victim_service_class, queue, tenant)
            jobs.append(
                _job(
                    run_id=args.run_id,
                    role=f"fair-{ordinal}",
                    ordinal=ordinal,
                    image=args.image,
                    model_id=args.model_id,
                    tenant_id=tenant,
                    decision=selected,
                    revision=policy.revision,
                    parallelism=args.parallelism,
                    minimum_parallelism=None,
                    hold_seconds=args.hold_seconds,
                    force_pool=args.pool_id is not None,
                )
            )
    elif args.scenario == "partial-admission":
        if args.minimum_parallelism is None:
            raise ContractError("partial-admission requires --minimum-parallelism")
        selected = decision(args.victim_service_class, args.queue_a, args.tenant_a)
        jobs.append(
            _job(
                run_id=args.run_id,
                role="partial",
                ordinal=1,
                image=args.image,
                model_id=args.model_id,
                tenant_id=args.tenant_a,
                decision=selected,
                revision=policy.revision,
                parallelism=args.parallelism,
                minimum_parallelism=args.minimum_parallelism,
                hold_seconds=args.hold_seconds,
                force_pool=args.pool_id is not None,
            )
        )
    elif args.scenario == "scale-zero":
        if args.pool_id is None:
            raise ContractError("scale-zero requires an explicit --pool-id")
        selected = decision(args.victim_service_class, args.queue_a, args.tenant_a)
        jobs.append(
            _job(
                run_id=args.run_id,
                role="scale-zero",
                ordinal=1,
                image=args.image,
                model_id=args.model_id,
                tenant_id=args.tenant_a,
                decision=selected,
                revision=policy.revision,
                parallelism=1,
                minimum_parallelism=None,
                hold_seconds=args.hold_seconds,
                force_pool=True,
            )
        )
    else:  # pragma: no cover - argparse owns this boundary
        raise ContractError("unsupported scenario")
    return {"apiVersion": "v1", "kind": "List", "items": jobs}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--contract", type=Path, required=True)
    result.add_argument(
        "--contract-sha256",
        required=True,
        help="sha256 from the workloads scheduling_contract_ref; the applied ConfigMap bytes must match it",
    )
    result.add_argument(
        "--scenario",
        choices=("victims", "preemptor", "fairness", "partial-admission", "scale-zero"),
        required=True,
    )
    result.add_argument("--run-id", required=True)
    result.add_argument("--image", required=True)
    result.add_argument("--model-id", required=True)
    result.add_argument("--tenant-a", required=True)
    result.add_argument("--tenant-b", default="tenant-b")
    result.add_argument("--queue-a")
    result.add_argument("--queue-b")
    result.add_argument("--pool-id")
    result.add_argument("--victim-service-class", default="bulk-backfill")
    result.add_argument("--preemptor-service-class", default="presentation")
    result.add_argument("--parallelism", type=int, default=2)
    result.add_argument("--minimum-parallelism", type=int)
    result.add_argument("--hold-seconds", type=int, default=900)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        value = render(parser().parse_args(argv))
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(value, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
