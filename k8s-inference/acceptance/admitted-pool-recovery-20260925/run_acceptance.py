#!/usr/bin/env python3
"""Exact-client GROMACS acceptance for admitted, unscheduled pool recovery.

Optional Kubernetes mutations are ownership-checked affinity patches to a
never-started suspended Job belonging to this disposable tenant. No Node,
quota, queue, model, Pod, or Workload status is modified. An independently
reviewed optional CREATE injector is observed, never deployed, by this runner.
Credentials and native artifacts stay in the caller's private output directory.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2]
MD = ROOT / "models/molecular-dynamics"
PREFIX = "fs2.nebius.ai/"
POOL = "accelerator.fs2.nebius/pool-id"
FAILURE = "admitted_pool_unavailable"
TENANT = "admitted-pool-recovery-20260925"
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "preempted"}
SCHEMA = "fs2-serve.nebius.ai/admitted-pool-recovery-acceptance/v1"


class GateError(RuntimeError):
    """A stable code, never a raw API response or credential-bearing exception."""


def require(condition, code):
    if not condition:
        raise GateError(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    with open(path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def age(timestamp, instant=None):
    return ((instant or datetime.now(timezone.utc)) - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()


def attempts(status):
    return [{**attempt, "stage_id": stage["stage_id"]}
            for stage in status["batch"]["stages"] for attempt in stage["attempts"]]


def ordinary_policy(policy, tenant):
    require(policy.get("tenant_id") == tenant and tenant.startswith(TENANT), "wrong_disposable_tenant")
    require(policy.get("models") == ["gromacs"], "key_must_be_gromacs_scoped")
    scopes = set(policy.get("scopes", []))
    required = {"artifacts.write", "catalog.read", "inference.invoke", "mcp.invoke",
                "operations.read", "operations.result", "operations.cancel"}
    require(required <= scopes <= required | {"operations.acknowledge"}, "key_not_ordinary_customer")
    require(1 <= policy.get("max_concurrency", 0) <= 3, "unexpected_customer_concurrency")
    return {key: policy[key] for key in ("tenant_id", "principal_id", "scopes", "models", "max_concurrency")}


class Kube:
    def __init__(self, kubeconfig, context):
        self.command = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context,
                        "--request-timeout=15s"]

    def json(self, *arguments, stdin=None):
        result = subprocess.run([*self.command, *arguments], input=stdin, text=True,
                                capture_output=True, timeout=25)
        require(result.returncode == 0, "kubernetes_request_failed")
        return json.loads(result.stdout)

    def items(self, kind, namespace=None, selector=None):
        command = ["get", kind, "-o", "json"]
        if namespace:
            command += ["-n", namespace]
        if selector:
            command += ["-l", selector]
        return self.json(*command)["items"]

    def release(self, declared):
        required = {"source_revision", "control_plane_image", "runtime_image", "client_image"}
        require(required <= declared.keys(), "release_identity_incomplete")
        require(re.fullmatch(r"[0-9a-f]{40}", declared["source_revision"]), "source_revision_not_pinned")
        for key in required - {"source_revision"}:
            require(re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", declared[key]), "image_not_pinned")
        names = {"fs2-serve-control-plane", "fs2-serve-control-plane-model-controller"}
        deployments = [row for row in self.items("deployments", "fs2-system") if row["metadata"]["name"] in names]
        require({row["metadata"]["name"] for row in deployments} == names, "control_plane_deployment_missing")
        configurations = set()
        records = []
        for row in deployments:
            spec, status = row["spec"], row.get("status", {})
            images = [c["image"] for c in spec["template"]["spec"]["containers"]]
            require(images == [declared["control_plane_image"]], "deployed_control_plane_digest_differs")
            require(status.get("readyReplicas", 0) == spec["replicas"] == status.get("updatedReplicas", 0), "control_plane_not_ready")
            records.append({"name": row["metadata"]["name"], "uid": row["metadata"]["uid"],
                            "spec_sha256": digest(spec), "images": images, "replicas": spec["replicas"]})
            configurations.update(v["configMap"]["name"] for v in spec["template"]["spec"].get("volumes", []) if "configMap" in v)
        config_digests = []
        for name in sorted(configurations):
            config = self.json("get", "configmap", name, "-n", "fs2-system", "-o", "json")
            config_digests.append({"name": name, "sha256": digest({k: config.get(k, {}) for k in ("data", "binaryData")})})
        # Quotas are observed and hash-bound, never changed by this runner.
        queues = [{"kind": row["kind"], "name": row["metadata"]["name"], "spec_sha256": digest(row["spec"])}
                  for kind in ("clusterqueues", "resourceflavors") for row in self.items(kind)]
        return {"declared": declared, "deployments": sorted(records, key=lambda r: r["name"]),
                "configuration": config_digests, "scheduling_resources": sorted(queues, key=lambda r: (r["kind"], r["name"]))}

    def nodes(self):
        raw = self.json("get", "configmap", "cluster-autoscaler-status", "-n", "kube-system", "-o", "json")["data"]["status"]
        parsed = subprocess.run(["python3", "-c", "import json,sys,yaml; print(json.dumps(yaml.safe_load(sys.stdin.read())))"],
                                input=raw, text=True, capture_output=True, timeout=10)
        require(parsed.returncode == 0, "autoscaler_status_yaml_unreadable")
        groups = {group["name"]: group for group in json.loads(parsed.stdout)["nodeGroups"]}
        return [{"name": row["metadata"]["name"], "uid": row["metadata"]["uid"],
                 "pool": row["metadata"].get("labels", {}).get(POOL),
                 "node_group": row["metadata"].get("labels", {}).get("nebius.com/node-group-id"),
                 "autoscaler": groups.get(row["metadata"].get("labels", {}).get("nebius.com/node-group-id")),
                 "ready": next((c for c in row.get("status", {}).get("conditions", []) if c["type"] == "Ready"), None)}
                for row in self.items("nodes") if row["metadata"].get("labels", {}).get(POOL)]

    def owned(self, tenant, operation=None, known_job_uids=()):
        selector = PREFIX + "tenant-id=" + tenant
        if operation:
            selector += "," + PREFIX + "operation-id=" + str(UUID(operation))
        jobs, pods = (self.items(kind, "fs2-models", selector) for kind in ("jobs", "pods"))
        workloads = []
        for uid in sorted(set(known_job_uids) | {job["metadata"]["uid"] for job in jobs}):
            workloads += self.items("workloads", "fs2-models", "kueue.x-k8s.io/job-uid=" + uid)
        return jobs, pods, workloads


def dead_pool(nodes, pool, minimum_age=120, instant=None):
    selected = [node for node in nodes if node["pool"] == pool]
    if not selected:
        return False
    for node in selected:
        ready, autoscaler = node["ready"], node.get("autoscaler") or {}
        health, scaling = autoscaler.get("health", {}), autoscaler.get("scaleUp", {})
        counts = health.get("nodeCounts", {})
        registered = counts.get("registered", {})
        if not (ready and ready["status"] == "Unknown" and ready.get("reason") == "NodeStatusUnknown"
                and age(ready["lastTransitionTime"], instant) >= minimum_age
                and health.get("status") == "Unhealthy" and health.get("lastProbeTime")
                and 0 <= age(health["lastProbeTime"], instant) <= 120
                and scaling.get("status") == "Unhealthy" and registered.get("ready") == 0
                and registered.get("notStarted") == 0 and counts.get("unregistered") == 0
                and counts.get("longUnregistered") == 0
                and health.get("cloudProviderTarget") == registered.get("total")):
            return False
    return True


def injection_patch(job, pods, workloads, tenant, operation, dead):
    """A racing resourceVersion change invalidates the entire atomic JSON patch."""
    meta, spec = job["metadata"], job["spec"]
    labels = meta.get("labels", {})
    require(tenant.startswith(TENANT) and labels.get(PREFIX + "tenant-id") == tenant, "injection_wrong_tenant")
    require(labels.get(PREFIX + "operation-id") == str(UUID(operation)), "injection_wrong_operation")
    require(labels.get(PREFIX + "model-id") == "gromacs" and labels.get(PREFIX + "stage-id") == "workflow", "injection_wrong_stage")
    require(bool(labels.get(PREFIX + "attempt-id")) and "-a1-" in meta["name"], "injection_not_first_attempt")
    require(spec.get("suspend") is True and not job.get("status", {}).get("startTime"), "injection_missed_suspend")
    require(not meta.get("deletionTimestamp"), "injection_job_deleting")
    require(not any(any(o.get("uid") == meta["uid"] for o in p["metadata"].get("ownerReferences", [])) for p in pods), "injection_pod_already_exists")
    require(not any(any(o.get("uid") == meta["uid"] for o in w["metadata"].get("ownerReferences", [])) for w in workloads), "injection_workload_already_exists")
    preference = meta.get("annotations", {}).get(PREFIX + "pool-preference", "").split(",")
    require(dead in preference and len(preference) >= 2, "injection_dead_pool_not_eligible")
    affinity = copy.deepcopy(spec["template"]["spec"].get("affinity", {}))
    node = affinity.setdefault("nodeAffinity", {})
    terms = node.setdefault("requiredDuringSchedulingIgnoredDuringExecution", {}).setdefault("nodeSelectorTerms", [{}])
    require(bool(terms), "injection_affinity_matches_no_nodes")
    for term in terms:
        expressions = term.setdefault("matchExpressions", [])
        for expression in expressions:
            if expression["key"] == POOL:
                require(expression["operator"] == "In" and dead in expression["values"], "injection_incompatible_affinity")
        expressions.append({"key": POOL, "operator": "In", "values": [dead]})
    tests = [{"op": "test", "path": "/metadata/" + field, "value": meta[field]}
             for field in ("uid", "resourceVersion", "name", "namespace")]
    tests += [{"op": "test", "path": "/spec/suspend", "value": True}]
    return tests + [{"op": "add", "path": "/spec/template/spec/affinity", "value": affinity}]


def synthetic_return_patch(job, pods, workloads, tenant, operation, shard):
    """Remove only the task injector's impossible predicate, never a reservation."""
    meta, spec = job["metadata"], job["spec"]
    labels = meta["labels"]
    require(tenant == TENANT and labels.get(PREFIX + "tenant-id") == TENANT, "synthetic_wrong_tenant")
    require(labels.get(PREFIX + "operation-id") == str(UUID(operation)) and labels.get(PREFIX + "shard-id") == shard,
            "synthetic_wrong_operation_or_shard")
    suffix = hashlib.sha256(f"{operation}:workflow:{shard}:2".encode()).hexdigest()[:12]
    require(meta["name"] == f"fs2-workflow-{shard}-a2-{suffix}" and meta["namespace"] == "fs2-models"
            and labels.get(PREFIX + "model-id") == "gromacs" and labels.get(PREFIX + "stage-id") == "workflow", "synthetic_wrong_attempt")
    require(spec.get("suspend") is True and not job.get("status", {}).get("startTime")
            and not meta.get("deletionTimestamp"), "synthetic_not_pristine_suspended")
    require(not any(any(o.get("uid") == meta["uid"] for o in p["metadata"].get("ownerReferences", [])) for p in pods), "synthetic_pod_exists")
    owned = [w for w in workloads if any(o.get("uid") == meta["uid"] for o in w["metadata"].get("ownerReferences", []))]
    require(owned and all(not w.get("status", {}).get("admission") and not any(c.get("type") in {"Admitted", "QuotaReserved"}
            and c.get("status") == "True" for c in w.get("status", {}).get("conditions", [])) for w in owned), "synthetic_quota_or_admission_exists")
    terms = spec["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    constraint = {"key": POOL, "operator": "In", "values": ["acceptance-no-capacity-20260925"]}
    preference = meta["annotations"][PREFIX + "pool-preference"].split(",")
    require(terms and "h100-1x" not in preference, "synthetic_retry_exclusion_missing")
    patch = [{"op": "test", "path": "/metadata/" + key, "value": meta[key]} for key in ("uid", "resourceVersion", "name", "namespace")]
    patch.append({"op": "test", "path": "/spec/suspend", "value": True})
    for index, term in enumerate(terms):
        expressions = term["matchExpressions"]
        require(expressions.count(constraint) == 1 and any(e.get("key") == POOL and e.get("operator") == "In"
                and e.get("values") and set(e["values"]) <= set(preference) for e in expressions), "synthetic_predicate_not_exact")
        path = f"/spec/template/spec/affinity/nodeAffinity/requiredDuringSchedulingIgnoredDuringExecution/nodeSelectorTerms/{index}/matchExpressions/{expressions.index(constraint)}"
        patch.extend([{"op": "test", "path": path, "value": constraint}, {"op": "remove", "path": path}])
    return patch


def contains_subset(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and contains_subset(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(contains_subset(a, e) for a, e in zip(actual, expected))
    return actual == expected


def canonical_observed_resource(actual):
    actual = copy.deepcopy(actual)
    # The API omits an empty egress list on read. With policyTypes=Egress,
    # absence and [] both mean deny all; do not relax any nonempty rule.
    if actual.get("kind") == "NetworkPolicy" and "Egress" in actual.get("spec", {}).get("policyTypes", []):
        actual["spec"].setdefault("egress", [])
    return actual


def observe_injector(kube, path, release, windows):
    """Read-only, exact prepared source/config/selector identity and ready server."""
    plan = read(path)
    config = plan["config"]
    require(plan["schema"] == "admitted-pool-recovery-injector/v1" and config["tenant"] == TENANT
            and config["namespace"] == "fs2-models" and config["shard"] in windows, "injector_scope_changed")
    server_image = "docker.io/library/python:3.13.15-alpine3.23@sha256:a3180613a9708f1cd59aa79a3dd82e8a6d3f3199d1d6e2c467a63687518872d3"
    require(config["runtime_image"] == release["runtime_image"] and plan["server_image"] == server_image, "injector_release_changed")
    require(age(config["expires_at"]) < 0, "injector_expired")
    require(sha(Path(__file__).with_name("admission_injector.py")) == plan["source_sha256"], "injector_source_changed")
    observed = []
    for filename in ("namespace.json", "server.json", "webhook.json"):
        desired = read(path.parent / filename)
        require(digest(desired) == plan["resource_sha256"][filename], "injector_plan_changed")
        for item in desired.get("items", [desired]):
            command = ["get", item["kind"], item["metadata"]["name"], "-o", "json"]
            if item["metadata"].get("namespace"):
                command += ["-n", item["metadata"]["namespace"]]
            actual = canonical_observed_resource(kube.json(*command))
            require(contains_subset(actual, item), "injector_live_configuration_changed")
            if item["kind"] == "Deployment":
                state = actual.get("status", {})
                require(state.get("readyReplicas") == state.get("updatedReplicas") == 1, "injector_not_ready")
            observed.append({"kind": item["kind"], "name": item["metadata"]["name"], "uid": actual["metadata"]["uid"],
                             "sha256": digest({k: actual[k] for k in ("spec", "data", "webhooks", "automountServiceAccountToken") if k in actual})})
    return {"plan_sha256": sha(path), "config": config, "resources": observed}


def compact_snapshot(jobs, pods, workloads, nodes):
    result = {"at": now(), "nodes": nodes, "jobs": [], "pods": [], "workloads": []}
    for job in jobs:
        meta = job["metadata"]
        result["jobs"].append({"name": meta["name"], "uid": meta["uid"], "namespace": meta["namespace"],
            "created_at": meta["creationTimestamp"],
            "attempt_id": meta["labels"][PREFIX + "attempt-id"], "operation_id": meta["labels"][PREFIX + "operation-id"],
            "pool_preference": meta.get("annotations", {}).get(PREFIX + "pool-preference", "").split(","),
            "affinity": job["spec"]["template"]["spec"].get("affinity", {}),
            "suspend": job["spec"].get("suspend"), "deletion_timestamp": meta.get("deletionTimestamp")})
    for pod in pods:
        meta, status = pod["metadata"], pod.get("status", {})
        result["pods"].append({"name": meta["name"], "uid": meta["uid"], "attempt_id": meta["labels"][PREFIX + "attempt-id"],
            "node": pod["spec"].get("nodeName"), "phase": status.get("phase"),
            "init_pause": [{k: c.get(k) for k in ("name", "image", "command")} for c in pod["spec"].get("initContainers", []) if c["name"] == "acceptance-init-pause"],
            "conditions": [{k: c.get(k) for k in ("type", "status", "reason", "lastTransitionTime")} for c in status.get("conditions", [])],
            "containers": [{"name": c["name"], "image": c.get("image"), "image_id": c.get("imageID"),
                            "started": bool(c.get("state", {}).get("running") or c.get("state", {}).get("terminated")),
                            "state": {phase: {k: value.get(k) for k in ("startedAt", "finishedAt", "exitCode")} for phase, value in c.get("state", {}).items()}}
                           for field in ("initContainerStatuses", "containerStatuses") for c in status.get(field, [])]})
    for workload in workloads:
        meta, status = workload["metadata"], workload.get("status", {})
        result["workloads"].append({"name": meta["name"], "uid": meta["uid"], "owners": meta.get("ownerReferences", []),
            "admission": status.get("admission"),
            "insufficient_quota": any(c.get("type") == "QuotaReserved" and c.get("status") == "False"
                                      and "insufficient" in c.get("message", "").lower() for c in status.get("conditions", [])),
            "capacity_wait_conditions": [{k: c.get(k) for k in ("type", "status", "reason", "message")} for c in status.get("conditions", [])
                                         if c.get("type") in {"QuotaReserved", "Admitted"} and c.get("status") == "False"],
            "conditions": [{k: c.get(k) for k in ("type", "status", "reason", "lastTransitionTime")} for c in status.get("conditions", [])]})
    return result


async def mcp_call(endpoint, secret, name, arguments):
    # This is the already-installed MCP 2.2 client API used by the released CLI.
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + secret}, timeout=60, trust_env=False) as http:
        async with Client(streamable_http_client(endpoint, http_client=http), read_timeout_seconds=60) as client:
            response = (await client.call_tool(name, arguments)).model_dump(mode="json", by_alias=True)
    require(not response.get("isError"), "public_mcp_tool_failed")
    if response.get("structuredContent") is not None:
        return response["structuredContent"]
    texts = [item["text"] for item in response.get("content", []) if item.get("type") == "text"]
    require(len(texts) == 1, "public_mcp_result_not_json")
    return json.loads(texts[0])


def call(endpoint, secret, name, arguments):
    return asyncio.run(mcp_call(endpoint, secret, name, arguments))


def customer_command(args, output):
    return ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
            "--entrypoint", "/opt/scientific-client/bin/python", "--env", "SCIENTIFIC_MODELS_API_KEY",
            "--env", "SCIENTIFIC_MODELS_MCP_URL", "--mount", f"type=bind,src={args.fixture.resolve()},dst=/input,readonly",
            "--mount", f"type=bind,src={output.resolve()},dst=/receipt", args.release["client_image"],
            "/opt/bionemo/invoke-scientific-batch.py", "--model", "gromacs", "--tool", "submit_gromacs_workflow",
            "--operation", "run-workflow", "--source", "/input/input.tar.gz", "--parameters", "/receipt/parameters.json",
            "--entry-name", "gromacs-inputs", "--semantic-type", "gromacs-input-bundle/v1", "--media-type", "application/x-tar",
            "--compression", "gzip", "--output", "/receipt/customer", "--idempotency-key", args.idempotency_key,
            "--display-name", "Admitted pool recovery acceptance", "--wait-seconds", str(args.timeout_seconds), "--poll-seconds", "5"]


def prepare_parameters(fixture, window_ids):
    original = read(fixture / "request.json")
    require(original.get("schema") == "fs2-serve.nebius.ai/gromacs-workflow-request/v1", "fixture_not_gromacs_parameters")
    jobs = {job["id"]: job for job in original["jobs"]}
    require(2 <= len(window_ids) <= 8 and len(set(window_ids)) == len(window_ids), "require_distinct_real_window_batch")
    require(set(window_ids) <= jobs.keys() and all(re.fullmatch(r"window-\d{2}", w) for w in window_ids), "window_not_in_fixture")
    parameters = {**original, "jobs": [jobs[w] for w in window_ids], "output_destination": "platform-artifacts"}
    return parameters, {"input_sha256": sha(fixture / "input.tar.gz"), "original_request_sha256": sha(fixture / "request.json"),
                        "derived_request_sha256": digest(parameters), "windows": window_ids,
                        "scientific_job_objects_unchanged": True, "changes": ["select explicit jobs", "output_destination=platform-artifacts"]}


def verify_recovery(status, observations, dead, failure=FAILURE):
    rows = attempts(status)
    recovered = []
    for first in rows:
        if first.get("failure_code") != failure:
            continue
        require(first.get("failure_kind") == "infrastructure" and first["resource_released"], "failed_attempt_not_released")
        require(first["scheduling_admission"]["resolved_pool_id"] == dead, "failed_attempt_wrong_pool")
        following = [r for r in rows if r["stage_id"] == first["stage_id"] and r["shard_id"] == first["shard_id"]
                     and r["attempt_number"] == first["attempt_number"] + 1]
        require(len(following) == 1, "missing_bounded_replacement")
        second = following[0]
        require(second["outcome"] == "succeeded" and second["resource_released"], "replacement_not_successful")
        require(second["scheduling_admission"]["resolved_pool_id"] != dead, "replacement_reused_dead_pool")
        recovery = first.get("recovery") or {}
        require(recovery.get("state") == "recovered" and recovery.get("cause") == failure
                and recovery.get("failed_pool_id") == dead and dead in recovery.get("avoided_pool_ids", [])
                and dead not in recovery.get("eligible_pool_ids", []) and recovery.get("admitted_wait_seconds", 0) >= 120
                and second["attempt_number"] <= recovery.get("max_attempts", 0), "public_recovery_explanation_incomplete")
        before = [o for o in observations if any(p["attempt_id"] == first["attempt_id"] and not p["node"]
                  and not any(c["started"] for c in p["containers"]) for p in o["pods"])
                  and dead_pool(o["nodes"], dead, instant=datetime.fromisoformat(o["at"]))
                  and age(first["scheduling_admission"]["admitted_at"], datetime.fromisoformat(o["at"])) >= 115]
        # Polls can fall just before the controller's 120 s observation; the
        # definitive failure is persisted by that controller, not this runner.
        require(before, "no_observed_dead_pool_unscheduled_pod")
        replacements = [o for o in observations if any(j["attempt_id"] == second["attempt_id"] for j in o["jobs"])]
        require(replacements, "replacement_manifest_not_observed")
        retry_times = [r["recovery"]["retry_not_before"] for o in observations if o.get("public_status")
                       for r in attempts(o["public_status"]) if r["attempt_id"] == first["attempt_id"]
                       and r.get("recovery", {}).get("retry_not_before")]
        require(retry_times, "public_retry_backoff_not_observed")
        for snapshot in replacements:
            replacement = next(j for j in snapshot["jobs"] if j["attempt_id"] == second["attempt_id"])
            require(all(datetime.fromisoformat(replacement["created_at"].replace("Z", "+00:00")) >=
                        datetime.fromisoformat(t.replace("Z", "+00:00")) for t in retry_times), "replacement_started_before_backoff")
            require(dead not in replacement["pool_preference"], "replacement_did_not_persist_pool_exclusion")
            require(all(j["attempt_id"] != first["attempt_id"] for j in snapshot["jobs"]), "job_replacement_overlaps_failed_attempt")
            require(all(p["attempt_id"] != first["attempt_id"] for p in snapshot["pods"]), "pod_replacement_overlaps_failed_attempt")
            require(all(all(owner.get("uid") != first["workload_uid"] for owner in w["owners"]) for w in snapshot["workloads"]), "quota_reservation_overlaps_replacement")
        recovered.append({"stage_id": first["stage_id"], "shard_id": first["shard_id"], "failed_attempt": first["attempt_id"],
                          "replacement_attempt": second["attempt_id"], "replacement_pool": second["scheduling_admission"]["resolved_pool_id"]})
    require(recovered, "no_admitted_pool_recovery_exercised")
    require(all(r["resource_released"] for r in rows), "terminal_resources_not_released")
    return recovered


def verify_init_pause(status, observations, config):
    """Require a real scheduled init >150 s and the same successful retry."""
    candidates = [r for r in attempts(status) if r["stage_id"] == "workflow" and r["shard_id"] == config["shard"] and r["attempt_number"] == 2]
    require(len(candidates) == 1 and candidates[0]["outcome"] == "succeeded" and not candidates[0].get("failure_code"), "paused_retry_not_successful")
    attempt = candidates[0]
    require(not any(r["stage_id"] == "workflow" and r["shard_id"] == config["shard"] and r["attempt_number"] > 2 for r in attempts(status)), "paused_retry_was_evicted")
    proof = []
    for snapshot in observations:
        for pod in snapshot["pods"]:
            if pod["attempt_id"] != attempt["attempt_id"] or not pod["node"]:
                continue
            specs = pod.get("init_pause", [])
            require(len(specs) == 1 and specs[0]["image"] == config["runtime_image"]
                    and specs[0]["command"] == ["/bin/sleep", str(config["pause_seconds"])], "init_pause_not_exact_runtime")
            for container in pod["containers"]:
                if container["name"] != "acceptance-init-pause":
                    continue
                state = container.get("state", {}).get("terminated", {})
                if state.get("exitCode") == 0 and state.get("startedAt") and state.get("finishedAt"):
                    elapsed = age(state["startedAt"], datetime.fromisoformat(state["finishedAt"].replace("Z", "+00:00")))
                    if elapsed >= config["pause_seconds"]:
                        proof.append({"pod_uid": pod["uid"], "node": pod["node"], "seconds": elapsed, "image_id": container["image_id"]})
    require(proof and all(p["image_id"] for p in proof), "full_scheduled_init_pause_not_observed")
    return {"state": "passed", "attempt_id": attempt["attempt_id"], "seconds": min(p["seconds"] for p in proof),
            "pod_uids": sorted({p["pod_uid"] for p in proof}), "actual_scale_from_zero_proven": False,
            "scope": "Injected same-image initialization survived; no physical scale-from-zero claim"}


def synthetic_wait_candidate(rows, snapshot, config):
    waiting = [r for r in rows if r["stage_id"] == "workflow" and r["shard_id"] == config["shard"]
               and r["attempt_number"] == 2 and r["outcome"] == "active" and not r.get("scheduling_admission")]
    if len(waiting) != 1:
        return None
    row = waiting[0]
    workloads = [w for w in snapshot["workloads"] if any(o.get("uid") == row["workload_uid"] for o in w["owners"])]
    if len(workloads) != 1 or workloads[0]["admission"]:
        return None
    reasons = [c for c in workloads[0].get("capacity_wait_conditions", []) if c.get("type") == "QuotaReserved"
               and c.get("status") == "False" and "affinity" in c.get("message", "").lower()]
    return {"attempt_id": row["attempt_id"], "job_uid": row["workload_uid"], "workload_uid": workloads[0]["uid"], "reasons": reasons} if reasons else None


def capture_accounting(kube, origin, tenant, status):
    """Read the existing operator surfaces; export only explicit safe fields."""
    import httpx
    operation = status["operation"]["id"]
    secret = kube.json("get", "secret", "fs2-serve-admin", "-n", "fs2-system", "-o", "json")
    token = base64.b64decode(secret["data"]["token"]).decode().strip()

    def checked(response):
        require(response.is_success, "operator_accounting_read_failed")
        return response.json()

    with httpx.Client(base_url=origin, timeout=60, trust_env=False, headers={"origin": origin}) as client:
        checked(client.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + token}))
        try:
            detail = checked(client.get("/admin/api/v1/scientific-runs/" + operation))["data"]
            require(detail["run"]["id"] == operation and detail["run"]["attribution"]["tenant_id"] == tenant
                    and detail["payloads_exposed"] is False, "operator_run_attribution_mismatch")
            listing = checked(client.get("/admin/api/v1/telemetry/workloads",
                              params={"tenant_id": tenant, "operation_id": operation, "limit": 200}))["data"]
            rows = []
            for row in listing["items"]:
                subject = row["subject"]
                require(subject["tenant_id"] == tenant and subject["operation_id"] == operation, "ledger_subject_wrong_owner")
                item = checked(client.get("/admin/api/v1/telemetry/workloads/" + subject["subject_id"]))["data"]
                require(item["payloads_exposed"] is False and item["subject"]["subject_id"] == subject["subject_id"], "ledger_detail_mismatch")
                rows.append({"subject": {k: item["subject"].get(k) for k in
                             ("subject_id", "attempt_id", "operation_id", "workload_id", "tenant_id", "principal_id", "model_id", "model_revision")},
                             "rollup": item.get("rollup"), "correlations": item.get("correlations", [])})
        finally:
            client.delete("/admin/api/v1/session")
    maximum = detail["retry"]["max_attempts_per_stage"]
    require(all(r["attempt_number"] <= maximum for r in attempts(status)), "frozen_retry_budget_exceeded")
    indexed = {r["subject"]["attempt_id"]: r for r in rows}
    gpu_attempts = [r for r in attempts(status) if (r.get("scheduling_admission") or {}).get("accelerator_count", 0)]
    require(gpu_attempts and all(r["attempt_id"] in indexed for r in gpu_attempts), "lifecycle_attempt_coverage_incomplete")
    for attempt in gpu_attempts:
        item = indexed[attempt["attempt_id"]]
        rollup = item["rollup"]
        require(rollup and rollup["terminal"], "lifecycle_rollup_not_terminal")
        require(any(c.get("job_uid") == attempt["workload_uid"] for c in item["correlations"]), "lifecycle_job_correlation_missing")
        if attempt.get("failure_code") == FAILURE:
            require(not any(c.get("node_uid") or c.get("gpu_uuid") for c in item["correlations"]), "unscheduled_failure_has_device_allocation")
            require(rollup.get("scheduler_occupied_gpu_seconds") in (None, 0)
                    and rollup.get("active_gpu_seconds") in (None, 0), "unscheduled_reservation_presented_as_gpu_execution")
    accounting = detail["run"]["gpu_accounting"]
    reconciled = {}
    for public, ledger in (("allocated", "scheduler_occupied_gpu_seconds"), ("active", "active_gpu_seconds"),
                           ("quota_reserved", "quota_reserved_gpu_seconds"), ("device_allocated", "device_allocated_gpu_seconds")):
        value = accounting[public]
        values = [indexed[r["attempt_id"]]["rollup"].get(ledger) for r in gpu_attempts]
        if value["value"] is not None and all(v is not None for v in values):
            require(abs(sum(values) - value["value"]) <= 1e-5, "operator_gpu_clock_disagrees_with_ledger")
            reconciled[public] = True
        else:
            require(value["value"] is None and value["evidence"] == "unavailable", "unknown_gpu_clock_invented")
            reconciled[public] = "unavailable"
    return {"operation_id": operation, "tenant_id": tenant, "retry": detail["retry"], "workloads": rows,
            "gpu_accounting": accounting, "clock_reconciliation": reconciled,
            "billing_claimed": False, "at": now(), "payloads_exposed": False}


def validate_native(args, output, parameters):
    validator = MD / "gromacs/qualification/validate_customer_results.py"
    result = subprocess.run([sys.executable, str(validator), "--receipt", str(output / "customer"),
                             "--parameters", str(output / "parameters.json"), "--output", str(output / "native-semantic.json")],
                            capture_output=True, timeout=300)
    require(result.returncode == 0, "native_semantic_gate_failed")
    spec = importlib.util.spec_from_file_location("materialize_native", MD / "materialize_customer_results.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.materialize("gromacs", output / "customer", output / "parameters.json", output / "native")
    for path in (output / "native").glob("*/data/**/*.log"):
        require(not re.search(r"(?im)^\s*(?:LINCS\s+)?WARNING\b|^\s*Fatal error", path.read_text(errors="replace")), "native_unexplained_warning")
    windows = [str(output / "native" / job["id"]) for job in parameters["jobs"]]
    command = [str(args.analysis_python), str(MD / "comparison/advanced-analysis/validate_umbrella_native.py"),
               "--windows", *windows, "--delivery", str(args.delivery), "--output", str(output / "native-integrity")]
    result = subprocess.run(command, capture_output=True, timeout=600)
    require(result.returncode == 0, "native_umbrella_integrity_gate_failed")
    science = read(output / "native-integrity/receipt.json")
    require(science["status"] == "passed" and science["frozen_reference_unchanged"], "native_umbrella_gate_incomplete")
    return {"semantic_sha256": sha(output / "native-semantic.json"), "integrity_sha256": sha(output / "native-integrity/receipt.json"),
            "window_count": len(windows), "scope": science["scope"], "convergence_claimed": False}


def run(args):
    import httpx
    os.umask(0o077)
    require(args.tenant.startswith(TENANT) and re.fullmatch(r"[a-z0-9-]+", args.tenant), "wrong_task_tenant")
    endpoint = urlsplit(args.endpoint)
    require(endpoint.scheme == "https" and endpoint.path == "/mcp" and not endpoint.query and not endpoint.fragment
            and not endpoint.username and not endpoint.password, "public_mcp_endpoint_invalid")
    origin = f"{endpoint.scheme}://{endpoint.netloc}"
    args.release = read(args.release_file)
    key = read(args.key_file)
    require(key.get("disposable") is True and key["key"]["tenant_id"] == args.tenant, "key_not_task_owned")
    secret = key["secret"]
    with httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + secret}, timeout=30, trust_env=False) as http:
        response = http.get("/v1/me")
        require(response.status_code == 200, "ordinary_identity_read_failed")
        policy = ordinary_policy(response.json(), args.tenant)
    kube = Kube(args.kubeconfig, args.context)
    release = kube.release(args.release)
    require(args.no_spare_seconds >= 120, "capacity_wait_proof_too_short")
    injector = observe_injector(kube, args.admission_injector_plan, args.release, args.windows) if args.admission_injector_plan else None
    require(not (injector and args.inject_dead_first_attempt), "injection_modes_conflict")
    synthetic = args.scenario == "synthetic-no-eligible-capacity"
    require(not synthetic or injector and injector["config"].get("synthetic_no_capacity"), "synthetic_injector_required")
    require(not injector or bool(injector["config"].get("synthetic_no_capacity")) == synthetic, "synthetic_injector_wrong_scenario")
    require(dead_pool(kube.nodes(), args.dead_pool), "dead_pool_not_confirmed_before_test")
    require(not any(kube.owned(args.tenant)), "prior_task_resources_require_inspection")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    output = args.output.resolve()
    parameters, fixture_identity = prepare_parameters(args.fixture, args.windows)
    save(output / "parameters.json", parameters)
    save(output / "release-before.json", release)
    save(output / "fixture.json", fixture_identity)
    receipt = {"schema": SCHEMA, "scenario": args.scenario, "state": "incomplete", "started_at": now(),
               "customer_ready": False, "endpoint": args.endpoint, "tenant_policy": policy, "key_id": key["key"]["id"],
               "release_sha256": digest(release), "release": args.release, "fixture": fixture_identity,
               "injection_mode": "task-only-create-webhook" if injector else "guarded-suspended-job-affinity" if args.inject_dead_first_attempt else "observe-natural",
               "injector": injector,
               "operation_id": None, "errors": [], "observations": 0}
    save(output / "intent.json", receipt)
    environment = {**os.environ, "SCIENTIFIC_MODELS_API_KEY": secret, "SCIENTIFIC_MODELS_MCP_URL": args.endpoint}
    observations, injected, cancelled, replayed = [], False, False, False
    known_job_uids = set()
    process = None
    deadline = time.monotonic() + args.timeout_seconds
    no_spare_started = None
    no_spare_proven = False
    synthetic_started, synthetic_returned = None, False
    last_status = None
    try:
        with (output / "client-private.log").open("xb") as client_log, (output / "observations.jsonl").open("x") as events:
            process = subprocess.Popen(customer_command(args, output), env=environment, stdout=client_log, stderr=subprocess.STDOUT)
            while time.monotonic() < deadline:
                submission = output / "customer/submission.json"
                if not receipt["operation_id"] and submission.is_file():
                    accepted = read(submission)
                    require(accepted["operation"]["tenant_id"] == args.tenant, "submission_wrong_owner")
                    receipt["operation_id"] = str(UUID(accepted["operation"]["id"]))
                    receipt["model_revision"] = accepted["operation"]["model_revision"]
                    save(output / "operation.json", {"operation_id": receipt["operation_id"], "at": now()})
                # Check owned suspended Jobs immediately, before slower public reads.
                jobs, pods, workloads = kube.owned(args.tenant, receipt["operation_id"], known_job_uids)
                known_job_uids.update(j["metadata"]["uid"] for j in jobs)
                if args.inject_dead_first_attempt and not injected:
                    candidates = [j for j in jobs if j["metadata"].get("labels", {}).get(PREFIX + "stage-id") == "workflow" and "-a1-" in j["metadata"]["name"]]
                    if candidates and receipt["operation_id"]:
                        job = candidates[0]
                        operation = job["metadata"]["labels"][PREFIX + "operation-id"]
                        require(operation == receipt["operation_id"], "injection_not_this_submission")
                        patch = injection_patch(job, pods, workloads, args.tenant, operation, args.dead_pool)
                        save(output / "injection-intent.json", {"at": now(), "job_uid": job["metadata"]["uid"], "operation_id": operation,
                            "attempt_id": job["metadata"]["labels"][PREFIX + "attempt-id"], "patch": patch})
                        kube.json("patch", "job", job["metadata"]["name"], "-n", "fs2-models", "--type=json", "--patch-file=/dev/stdin", "-o", "json", stdin=json.dumps(patch))
                        injected = True
                snapshot = compact_snapshot(jobs, pods, workloads, kube.nodes())
                observations.append(snapshot)
                events.write(json.dumps(snapshot) + "\n")
                events.flush()
                receipt["observations"] += 1
                operation = receipt["operation_id"]
                if not operation:
                    require(process.poll() is None, "customer_client_exited_before_acceptance")
                    time.sleep(.25)
                    continue
                if not replayed:
                    replay = call(args.endpoint, secret, "submit_gromacs_workflow", read(output / "customer/request.json"))
                    require(replay["operation"]["id"] == operation and replay["operation"].get("reused") is True, "idempotent_replay_created_work")
                    save(output / "replay.json", {"operation_id": operation, "workload_id": replay["batch"]["workload_id"], "reused": True, "at": now()})
                    replayed = True
                status = call(args.endpoint, secret, "get_scientific_status", {"operation_id": operation})
                require(status["operation"]["id"] == operation and status["operation"]["tenant_id"] == args.tenant, "public_status_wrong_owner")
                last_status = status
                snapshot["public_status"] = status
                known_job_uids.update(r["workload_uid"] for r in attempts(status) if r.get("workload_uid"))
                events.write(json.dumps({"at": now(), "public_status": status}) + "\n")
                events.flush()
                rows = attempts(status)
                if synthetic and not synthetic_returned:
                    candidate = synthetic_wait_candidate(rows, snapshot, injector["config"])
                    if candidate:
                        identity = tuple(sorted(r["attempt_id"] for r in rows))
                        if synthetic_started is None:
                            synthetic_started = (time.monotonic(), identity, candidate)
                        require(synthetic_started[1] == identity and synthetic_started[2]["job_uid"] == candidate["job_uid"]
                                and synthetic_started[2]["workload_uid"] == candidate["workload_uid"], "synthetic_wait_churn")
                        elapsed = time.monotonic() - synthetic_started[0]
                        if elapsed >= args.no_spare_seconds:
                            target = next(j for j in jobs if j["metadata"]["uid"] == candidate["job_uid"])
                            patch = synthetic_return_patch(target, pods, workloads, args.tenant, operation, injector["config"]["shard"])
                            evidence = {"at": now(), **candidate, "observed_wait_seconds": elapsed, "patch": patch,
                                        "kind": "synthetic-task-only-eligibility-loss-and-return", "physical_capacity_exhaustion_claimed": False}
                            save(output / "synthetic-return-intent.json", evidence)
                            kube.json("patch", "job", target["metadata"]["name"], "-n", "fs2-models", "--type=json", "--patch-file=/dev/stdin", "-o", "json", stdin=json.dumps(patch))
                            synthetic_returned = True
                            receipt["synthetic_capacity"] = evidence
                    elif synthetic_started:
                        raise GateError("synthetic_wait_interval_interrupted")
                selected = [r for r in rows if (r.get("scheduling_admission") or {}).get("resolved_pool_id") == args.dead_pool]
                if selected and args.scenario == "cancel" and not cancelled:
                    require(any(p["attempt_id"] in {r["attempt_id"] for r in selected} and not p["node"] for p in snapshot["pods"]), "cancellation_not_in_admitted_unscheduled_state")
                    call(args.endpoint, secret, "cancel_scientific_run", {"operation_id": operation})
                    cancelled = True
                if args.scenario == "no-spare":
                    failed = [r for r in rows if r.get("failure_code") == FAILURE and r["resource_released"]]
                    waiting = [r for r in rows if r["attempt_number"] > 1 and not r.get("scheduling_admission") and r["outcome"] == "active"]
                    waiting_uids = {r["workload_uid"] for r in waiting}
                    quota_wait = any(w["insufficient_quota"] and not w["admission"] and any(o.get("uid") in waiting_uids for o in w["owners"])
                                     for w in snapshot["workloads"])
                    if failed and waiting and quota_wait:
                        identity = tuple(sorted(r["attempt_id"] for r in rows))
                        if no_spare_started is None:
                            no_spare_started = (time.monotonic(), identity)
                        require(no_spare_started[1] == identity, "no_spare_retry_churn")
                        if time.monotonic() - no_spare_started[0] >= args.no_spare_seconds:
                            no_spare_proven = True
                            # The return-to-capacity outcome must still complete naturally.
                    elif no_spare_started and not no_spare_proven:
                        raise GateError("no_spare_interval_not_observed")
                if status["operation"]["status"] in TERMINAL and (status["operation"]["status"] != "succeeded" or status["batch"]["result_published"]):
                    break
                time.sleep(args.poll_seconds)
            require(last_status is not None, "no_public_status_observed")
            terminal = last_status["operation"]["status"]
            require(terminal in TERMINAL, "acceptance_deadline_exceeded")
            save(output / "terminal.json", last_status)
            if args.scenario == "cancel":
                require(cancelled and terminal == "cancelled" and all(r["resource_released"] for r in attempts(last_status)), "cancellation_cleanup_incomplete")
                receipt["cancellation_verified"] = True
            else:
                require(terminal == "succeeded", "customer_operation_unsuccessful")
                if args.scenario == "no-spare":
                    require(no_spare_proven, "no_spare_not_exercised")
                require(not synthetic or synthetic_returned, "synthetic_capacity_return_not_exercised")
                receipt["recovered_attempts"] = verify_recovery(last_status, observations, args.dead_pool)
                if injector and injector["config"]["pause_seconds"]:
                    receipt["init_pause"] = verify_init_pause(last_status, observations, injector["config"])
                started = [c for o in observations for p in o["pods"] for c in p["containers"]
                           if c["name"] == "scientific-stage" and c["started"]]
                require(started and all(c["image"] == args.release["runtime_image"] and c["image_id"] for c in started), "executed_runtime_digest_unproven")
                require(process.wait(timeout=600) == 0, "customer_artifact_client_failed")
                receipt["native"] = validate_native(args, output, parameters)
            require(not any(kube.owned(args.tenant, receipt["operation_id"], known_job_uids)), "owned_resources_leaked")
            accounting = capture_accounting(kube, origin, args.tenant, last_status)
            save(output / "accounting.json", accounting)
            receipt["accounting_sha256"] = sha(output / "accounting.json")
            receipt["frozen_max_attempts_per_stage"] = accounting["retry"]["max_attempts_per_stage"]
            after = kube.release(args.release)
            save(output / "release-after.json", after)
            require(after == release, "release_changed_during_cohort")
            if injector:
                require(observe_injector(kube, args.admission_injector_plan, args.release, args.windows) == injector, "injector_changed_during_cohort")
            receipt.update(state="passed", idempotent_replay_verified=replayed, no_spare_verified=no_spare_proven,
                           owned_resources_remaining=0, retained_control_plane_ready=True)
    except Exception as error:
        receipt["errors"].append(str(error) if isinstance(error, GateError) else type(error).__name__)
        receipt["state"] = "failed"
        # A patch race may precede the next receipt read. Recover the exact
        # saved acceptance before attempting cleanup; never guess an operation.
        if not receipt["operation_id"] and (output / "customer/submission.json").is_file():
            accepted = read(output / "customer/submission.json")
            if accepted["operation"].get("tenant_id") == args.tenant:
                receipt["operation_id"] = str(UUID(accepted["operation"]["id"]))
        # Cancel only the exact accepted operation created by this invocation.
        if receipt["operation_id"] and (not last_status or last_status["operation"]["status"] not in TERMINAL):
            try:
                call(args.endpoint, secret, "cancel_scientific_run", {"operation_id": receipt["operation_id"]})
                receipt["cleanup_cancellation_requested"] = True
                cleanup_deadline = time.monotonic() + 180
                while time.monotonic() < cleanup_deadline:
                    cleanup = call(args.endpoint, secret, "get_scientific_status", {"operation_id": receipt["operation_id"]})
                    known_job_uids.update(r["workload_uid"] for r in attempts(cleanup) if r.get("workload_uid"))
                    if cleanup["operation"]["status"] in TERMINAL and not any(kube.owned(args.tenant, receipt["operation_id"], known_job_uids)):
                        receipt["cleanup_verified"] = True
                        save(output / "cleanup.json", {"operation_id": receipt["operation_id"], "status": cleanup["operation"]["status"],
                                                       "owned_resources_remaining": 0, "at": now()})
                        break
                    time.sleep(2)
                require(receipt.get("cleanup_verified"), "cleanup_deadline_exceeded")
            except Exception:
                receipt["errors"].append("cleanup_cancel_failed")
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        receipt["finished_at"] = now()
        receipt["manual_recovery_performed"] = False
        save(output / "receipt.json", receipt)
    print(json.dumps({"state": receipt["state"], "operation_id": receipt["operation_id"], "errors": receipt["errors"]}))
    return 0 if receipt["state"] == "passed" else 1


def combine(paths):
    receipts = [read(path) for path in paths]
    require(len(receipts) >= 2 and all(r["state"] == "passed" and r["scenario"] == "recovery" for r in receipts), "two_clean_recovery_cohorts_required")
    require(len({r["operation_id"] for r in receipts}) == len(receipts), "duplicate_cohort_operation")
    for key in ("release_sha256", "release", "tenant_policy", "key_id", "model_revision", "fixture", "frozen_max_attempts_per_stage"):
        require(all(r[key] == receipts[0][key] for r in receipts), "cohort_identity_changed_" + key)
    for key in ("injection_mode", "injector"):
        require(all(r.get(key) == receipts[0].get(key) for r in receipts), "cohort_identity_changed_" + key)
    require(all(not r["errors"] and r["owned_resources_remaining"] == 0 and r["idempotent_replay_verified"] for r in receipts), "cohort_not_clean")
    require(all(datetime.fromisoformat(left["finished_at"]) <= datetime.fromisoformat(right["started_at"])
                for left, right in zip(receipts, receipts[1:])), "cohorts_not_consecutive")
    return {"schema": SCHEMA, "state": "two-clean-recovery-cohorts", "customer_ready": False,
            "scope": "Exact public MCP GROMACS window batch; whole-platform readiness, cloud node repair and scientific convergence are not established.",
            "release": receipts[0]["release"], "release_sha256": receipts[0]["release_sha256"],
            "operations": [r["operation_id"] for r in receipts], "receipts": [{"sha256": sha(p)} for p in paths],
            "remaining_gate": ["Separate cancellation/no-spare evidence and retained-service sibling checks must accompany this receipt."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--kubeconfig", type=Path, required=True)
    runner.add_argument("--context", required=True)
    runner.add_argument("--endpoint", default="https://89.169.99.188/mcp")
    runner.add_argument("--tenant", default=TENANT)
    runner.add_argument("--key-file", type=Path, required=True)
    runner.add_argument("--release-file", type=Path, required=True)
    runner.add_argument("--fixture", type=Path, required=True)
    runner.add_argument("--windows", nargs="+", default=["window-01", "window-02"])
    runner.add_argument("--analysis-python", type=Path, required=True)
    runner.add_argument("--delivery", type=Path, required=True)
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--idempotency-key", required=True)
    runner.add_argument("--scenario", choices=["recovery", "cancel", "no-spare", "synthetic-no-eligible-capacity"], default="recovery")
    runner.add_argument("--inject-dead-first-attempt", action="store_true")
    runner.add_argument("--admission-injector-plan", type=Path, help="Observe an explicitly reviewed/deployed optional injector; this runner never deploys it")
    runner.add_argument("--dead-pool", default="h100-1x")
    runner.add_argument("--timeout-seconds", type=float, default=2400)
    runner.add_argument("--poll-seconds", type=float, default=2)
    runner.add_argument("--no-spare-seconds", type=float, default=120)
    merger = sub.add_parser("combine")
    merger.add_argument("receipts", type=Path, nargs="+")
    merger.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "combine":
        value = combine(args.receipts)
        save(args.output, value)
        print(json.dumps(value))
        return 0
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as error:
        print(json.dumps({"state": "failed", "error": str(error)}))
        raise SystemExit(1) from None
