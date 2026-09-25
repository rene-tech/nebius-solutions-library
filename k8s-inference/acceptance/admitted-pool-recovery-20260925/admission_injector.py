#!/usr/bin/env python3
"""Fail-open, stateless CREATE-only injector for one disposable acceptance tenant.

This server has no Kubernetes client, token, API permissions, or external calls.
It returns JSON patches only; its exact selectors must also be enforced in the
MutatingWebhookConfiguration. Configuration and source are mounted read-only.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import ssl
from uuid import NAMESPACE_URL, UUID, uuid5

TENANT = "admitted-pool-recovery-20260925"
NAMESPACE = "fs2-models"
PREFIX = "fs2.nebius.ai/"
POOL = "accelerator.fs2.nebius/pool-id"
PAUSE = "acceptance-init-pause"
SYNTHETIC_POOL = "acceptance-no-capacity-20260925"
CONTROLLERS = (
    "system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime",
    "system:serviceaccount:fs2-system:fs2-serve-control-plane-controller",
)
IMAGE_PATTERN = r"[^\s]+@sha256:[0-9a-f]{64}"


class NotEligible(ValueError):
    """Safe reason code, never an object, key, runtime argument, or payload."""


def require(value, code):
    if not value:
        raise NotEligible(code)


def validate_config(config):
    require(config["tenant"] == TENANT and config["namespace"] == NAMESPACE, "scope_invalid")
    require(config["dead_pool"] == "h100-1x", "pool_invalid")
    require(re.fullmatch(IMAGE_PATTERN, config["runtime_image"]), "runtime_unpinned")
    require(re.fullmatch(r"window-0[1-8]", config["shard"]), "shard_invalid")
    seconds = config["pause_seconds"]
    require(type(seconds) is int and (seconds == 0 or 150 <= seconds <= 600), "pause_invalid")
    require(type(config.get("synthetic_no_capacity", False)) is bool, "synthetic_mode_invalid")
    require(not (seconds and config.get("synthetic_no_capacity")), "retry_modes_conflict")
    expires = datetime.fromisoformat(config["expires_at"])
    require(expires.tzinfo is not None, "expiry_invalid")
    return config


def expected_identity(operation, shard, number):
    operation = str(UUID(operation))
    workload = str(uuid5(NAMESPACE_URL, f"fs2-scientific-batch:{operation}:workload"))
    attempt = str(uuid5(NAMESPACE_URL, f"fs2-scientific-batch:{operation}:workflow:{shard}:{number}"))
    suffix = hashlib.sha256(f"{operation}:workflow:{shard}:{number}".encode()).hexdigest()[:12]
    return workload, attempt, f"fs2-workflow-{shard}-a{number}-{suffix}"


def pause_container(runtime_image, seconds):
    # Both requests AND limits are below prepare-workspace's existing requests
    # and limits, so this sequential, non-restartable init cannot enlarge the
    # PodSet's effective reservation. No mounts, env, network, or GPU requests.
    return {
        "name": PAUSE, "image": runtime_image, "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/sleep", str(seconds)],
        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                      "limits": {"cpu": "50m", "memory": "32Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
    }


def mutation(review, config, instant=None):
    """Return patch + safe reason. Every guard refuses injection, never the Job."""
    validate_config(config)
    require(review.get("apiVersion") == "admission.k8s.io/v1" and review.get("kind") == "AdmissionReview", "wrong_review")
    request = review["request"]
    require(request.get("operation") == "CREATE" and not request.get("subResource"), "not_job_create")
    require(request.get("resource") == {"group": "batch", "version": "v1", "resource": "jobs"}, "wrong_resource")
    require(request.get("namespace") == NAMESPACE, "wrong_namespace")
    require(request.get("userInfo", {}).get("username") in CONTROLLERS, "wrong_creator")
    require((instant or datetime.now(timezone.utc)) < datetime.fromisoformat(config["expires_at"]), "injector_expired")
    job = request["object"]
    require(job.get("apiVersion") == "batch/v1" and job.get("kind") == "Job", "wrong_kind")
    meta, spec = job["metadata"], job["spec"]
    require(meta.get("namespace") == NAMESPACE and not meta.get("deletionTimestamp"), "wrong_object_namespace")
    labels, annotations = meta["labels"], meta["annotations"]
    expected_labels = {"tenant-id": TENANT, "model-id": "gromacs", "stage-id": "workflow", "shard-id": config["shard"]}
    require(all(labels.get(PREFIX + key) == value for key, value in expected_labels.items()), "wrong_owner_or_stage")
    match = re.fullmatch(r"fs2-workflow-" + re.escape(config["shard"]) + r"-a([12])-[0-9a-f]{12}", meta["name"])
    require(match, "not_first_or_second_attempt")
    number = int(match[1])
    workload, attempt, name = expected_identity(labels[PREFIX + "operation-id"], config["shard"], number)
    require(labels.get(PREFIX + "workload-id") == workload and labels.get(PREFIX + "attempt-id") == attempt
            and meta["name"] == name, "deterministic_identity_mismatch")
    require(spec.get("suspend") is True and spec.get("backoffLimit") == 0 and not job.get("status"), "job_not_pristine_suspended")
    template = spec["template"]
    for key in (*expected_labels, "operation-id", "workload-id", "attempt-id"):
        require(template.get("metadata", {}).get("labels", {}).get(PREFIX + key) == labels[PREFIX + key], "template_owner_mismatch")
    pod = template["spec"]
    require(not pod.get("nodeName") and pod.get("restartPolicy") == "Never", "pod_already_placed")
    require(annotations.get(PREFIX + "accelerator-resource") == "nvidia.com/gpu"
            and annotations.get(PREFIX + "accelerator-count") == "1", "not_single_gpu_stage")
    for key in ("scientific-manifest-sha256", "podset-resource-envelope-sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", annotations.get(PREFIX + key, "")), "missing_frozen_digest")
    require(annotations.get(PREFIX + "podset-resource-envelope"), "missing_frozen_envelope")
    stages = [c for c in pod["containers"] if c["name"] == "scientific-stage"]
    require(len(stages) == 1 and stages[0]["image"] == config["runtime_image"], "runtime_changed")
    require(all(str(stages[0]["resources"][kind].get("nvidia.com/gpu")) == "1" for kind in ("requests", "limits")), "gpu_request_changed")
    preference = annotations[PREFIX + "pool-preference"].split(",")
    require(len(preference) == len(set(preference)) and all(preference), "pool_preference_invalid")
    dead = config["dead_pool"]
    if number == 1:
        require(dead in preference and len(preference) >= 2, "dead_pool_not_originally_qualified")
        require(pod.get("nodeSelector", {}).get(POOL, dead) == dead, "conflicting_node_selector")
        affinity = copy.deepcopy(pod.get("affinity", {}))
        terms = affinity.setdefault("nodeAffinity", {}).setdefault("requiredDuringSchedulingIgnoredDuringExecution", {}).setdefault("nodeSelectorTerms", [{}])
        require(terms, "affinity_matches_no_nodes")
        constraint = {"key": POOL, "operator": "In", "values": [dead]}
        for term in terms:
            expressions = term.setdefault("matchExpressions", [])
            require(all(e.get("operator") == "In" and dead in e.get("values", []) for e in expressions if e.get("key") == POOL), "conflicting_pool_affinity")
            if constraint not in expressions:
                expressions.append(copy.deepcopy(constraint))
        if affinity == pod.get("affinity"):
            return [], "dead_affinity_already_present"
        return [{"op": "add", "path": "/spec/template/spec/affinity", "value": affinity}], "dead_affinity_injected"
    require(dead not in preference, "retry_did_not_exclude_dead_pool")
    if config.get("synthetic_no_capacity"):
        affinity = copy.deepcopy(pod.get("affinity", {}))
        terms = affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        require(terms and SYNTHETIC_POOL not in preference, "synthetic_pool_invalid")
        constraint = {"key": POOL, "operator": "In", "values": [SYNTHETIC_POOL]}
        for term in terms:
            expressions = term["matchExpressions"]
            # Retain the qualified set too, making the conjunction impossible
            # even if somebody later creates a node with the synthetic label.
            require(any(e.get("key") == POOL and e.get("operator") == "In" and e.get("values")
                        and set(e["values"]) <= set(preference) for e in expressions), "qualified_affinity_missing")
            if constraint not in expressions:
                expressions.append(copy.deepcopy(constraint))
        if affinity == pod.get("affinity"):
            return [], "synthetic_barrier_already_present"
        return [{"op": "add", "path": "/spec/template/spec/affinity", "value": affinity}], "synthetic_barrier_injected"
    require(config["pause_seconds"] > 0, "retry_pause_disabled")
    initializers = pod.get("initContainers", [])
    # Anchor the resource-envelope argument to the actual renderer, rather than
    # assuming a tiny init is harmless for arbitrary Jobs.
    prepare = next((c for c in initializers if c["name"] == "prepare-workspace"), None)
    require(prepare and prepare.get("resources") == {
        "requests": {"cpu": "50m", "memory": "64Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"}}, "unexpected_initialization_envelope")
    pause = pause_container(config["runtime_image"], config["pause_seconds"])
    existing = [c for c in initializers if c["name"] == PAUSE]
    if existing:
        # Kubernetes may add terminationMessage* defaults between invocations.
        require(len(existing) == 1 and all(existing[0].get(k) == v for k, v in pause.items()), "conflicting_init_pause")
        return [], "init_pause_already_present"
    return [{"op": "add", "path": "/spec/template/spec/initContainers/0", "value": pause}], "init_pause_injected"


def admit(review, config, instant=None):
    request = review.get("request") if isinstance(review, dict) else None
    uid = request.get("uid", "") if isinstance(request, dict) else ""
    response = {"uid": uid, "allowed": True}
    try:
        patch, reason = mutation(review, config, instant)
        if patch:
            response.update(patchType="JSONPatch", patch=base64.b64encode(json.dumps(patch, separators=(",", ":")).encode()).decode())
    except NotEligible as error:
        reason = str(error)
    except (KeyError, TypeError, ValueError, AttributeError):
        reason = "malformed_or_unrecognized_object"
    response["auditAnnotations"] = {"acceptance.fs2.nebius.ai/injection": reason}
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": response}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        # Default request logs can contain arbitrary paths. Do not log objects,
        # runtime env, capabilities, request bodies, headers, or private URLs.
        pass

    def send_json(self, status, document):
        body = json.dumps(document, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_json(200 if self.path == "/healthz" else 404, {"ok": self.path == "/healthz"})

    def do_POST(self):
        self.connection.settimeout(2)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/mutate" or not 0 < length <= 2_000_000:
                self.send_json(400, {"error": "invalid_admission_request"})
                return
            review = json.loads(self.rfile.read(length))
            self.send_json(200, admit(review, self.server.injector_config))
        except (ValueError, OSError):
            self.close_connection = True


class AdmissionServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address


def serve():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    config = validate_config(json.loads(args.config.read_text()))
    server = AdmissionServer(("0.0.0.0", args.port), Handler)
    server.injector_config = config
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(str(args.cert), str(args.key))
    server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    serve()
