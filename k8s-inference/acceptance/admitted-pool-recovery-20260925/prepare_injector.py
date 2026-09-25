#!/usr/bin/env python3
"""Render the optional injector and short-lived TLS locally. Never calls kubectl."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess

import admission_injector as injector

NAME = "admitted-pool-recovery-injector-20260925"
SERVER_NAMESPACE = injector.TENANT
OWNER = "acceptance.fs2.nebius.ai/task"
SERVER_IMAGE = "docker.io/library/python:3.13.15-alpine3.23@sha256:a3180613a9708f1cd59aa79a3dd82e8a6d3f3199d1d6e2c467a63687518872d3"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def instance_identity(instance):
    injector.require(instance in ("recovery", "init", "synthetic"), "unknown_injector_instance")
    suffix = "" if instance == "recovery" else "-" + instance
    return NAME + suffix, SERVER_NAMESPACE + suffix


def bundle(config, server_image, source, ca, cert, key, ingress_cidrs=(), instance="recovery"):
    injector.validate_config(config)
    injector.require(re.fullmatch(injector.IMAGE_PATTERN, server_image), "server_image_unpinned")
    name, namespace_name = instance_identity(instance)
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": injector.TENANT, OWNER: injector.TENANT}

    def resource(api, kind, resource_name=name, namespaced=True, **fields):
        meta = {"name": resource_name, "labels": dict(labels)}
        if namespaced:
            meta["namespace"] = namespace_name
        return {"apiVersion": api, "kind": kind, "metadata": meta, **fields}

    namespace = resource("v1", "Namespace", namespace_name, namespaced=False)
    namespace["metadata"]["labels"].update({"pod-security.kubernetes.io/enforce": "restricted"})
    secret = resource("v1", "Secret", name + "-tls", type="kubernetes.io/tls", immutable=True,
                      data={"tls.crt": base64.b64encode(cert).decode(), "tls.key": base64.b64encode(key).decode()})
    account = resource("v1", "ServiceAccount", automountServiceAccountToken=False)
    code = resource("v1", "ConfigMap", name + "-code", immutable=True,
                    data={"admission_injector.py": source, "config.json": json.dumps(config, sort_keys=True)})
    service = resource("v1", "Service", spec={"type": "ClusterIP", "selector": labels,
        "ports": [{"name": "https", "port": 443, "targetPort": "https", "protocol": "TCP"}]})
    probe = {"httpGet": {"path": "/healthz", "port": "https", "scheme": "HTTPS"}, "timeoutSeconds": 1}
    pod = {"serviceAccountName": name, "automountServiceAccountToken": False, "enableServiceLinks": False,
        "terminationGracePeriodSeconds": 5,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                            "fsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
        # The injector never reserves a GPU or competes for a GPU node slot.
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": injector.POOL, "operator": "DoesNotExist"}]}]}}},
        "containers": [{"name": "admission", "image": server_image, "imagePullPolicy": "IfNotPresent",
            "command": ["/usr/local/bin/python", "-I", "/injector/admission_injector.py"],
            "args": ["--config", "/injector/config.json", "--cert", "/tls/tls.crt", "--key", "/tls/tls.key"],
            "ports": [{"name": "https", "containerPort": 8443, "protocol": "TCP"}],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "96Mi"}},
            "startupProbe": {**probe, "periodSeconds": 2, "failureThreshold": 30},
            "readinessProbe": {**probe, "periodSeconds": 2, "failureThreshold": 2},
            "livenessProbe": {**probe, "periodSeconds": 10, "failureThreshold": 3},
            "volumeMounts": [{"name": "code", "mountPath": "/injector", "readOnly": True}, {"name": "tls", "mountPath": "/tls", "readOnly": True}]}],
        "volumes": [{"name": "code", "configMap": {"name": name + "-code", "defaultMode": 0o444}},
                    {"name": "tls", "secret": {"secretName": name + "-tls", "defaultMode": 0o440}}]}
    deployment = resource("apps/v1", "Deployment", spec={"replicas": 1, "revisionHistoryLimit": 0,
        "strategy": {"type": "Recreate"}, "progressDeadlineSeconds": 180,
        "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": pod}})
    ingress = {"ports": [{"protocol": "TCP", "port": 8443}]}
    if ingress_cidrs:
        ingress["from"] = [{"ipBlock": {"cidr": str(ipaddress.ip_network(cidr))}} for cidr in ingress_cidrs]
    # No API, DNS, Internet, or other outbound access is needed. API-server
    # ingress may be narrowed when its actual source CIDR is known; guessing a
    # source range would silently disable this fail-open acceptance injector.
    network = resource("networking.k8s.io/v1", "NetworkPolicy", spec={"podSelector": {"matchLabels": labels},
        "policyTypes": ["Ingress", "Egress"], "ingress": [ingress], "egress": []})
    webhook = resource("admissionregistration.k8s.io/v1", "MutatingWebhookConfiguration", namespaced=False, webhooks=[{
        "name": name + ".acceptance.fs2.nebius.ai", "admissionReviewVersions": ["v1"], "sideEffects": "None",
        "failurePolicy": "Ignore", "timeoutSeconds": 2, "reinvocationPolicy": "IfNeeded", "matchPolicy": "Exact",
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": injector.NAMESPACE}},
        "objectSelector": {"matchLabels": {injector.PREFIX + "tenant-id": injector.TENANT,
            injector.PREFIX + "model-id": "gromacs", injector.PREFIX + "stage-id": "workflow", injector.PREFIX + "shard-id": config["shard"]}},
        "matchConditions": [{"name": "exact-controller-creator", "expression": "request.userInfo.username in " + json.dumps(list(injector.CONTROLLERS))}],
        "rules": [{"apiGroups": ["batch"], "apiVersions": ["v1"], "resources": ["jobs"], "operations": ["CREATE"], "scope": "Namespaced"}],
        "clientConfig": {"caBundle": base64.b64encode(ca).decode(),
                         "service": {"namespace": namespace_name, "name": name, "port": 443, "path": "/mutate"}},
    }])
    return {"namespace.json": namespace, "tls-secret-private.json": secret,
            "server.json": {"apiVersion": "v1", "kind": "List", "items": [account, code, service, network, deployment]},
            "webhook.json": webhook}


def certificates(output, instance="recovery"):
    """Root + service leaf, 24-hour lifetime, keys never emitted to stdout."""
    name, namespace = instance_identity(instance)
    dns = name + "." + namespace + ".svc"
    commands = [
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "1", "-subj", "/CN=" + name + "-ca",
         "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-keyout", "ca-private.key", "-out", "ca.crt"],
        ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", "/CN=" + name,
         "-addext", "subjectAltName=DNS:" + dns + ",DNS:" + dns + ".cluster.local",
         "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
         "-addext", "extendedKeyUsage=serverAuth", "-keyout", "tls-private.key", "-out", "tls.csr"],
        ["x509", "-req", "-in", "tls.csr", "-CA", "ca.crt", "-CAkey", "ca-private.key", "-set_serial", "1",
         "-days", "1", "-sha256", "-copy_extensions", "copy", "-out", "tls.crt"],
        ["verify", "-CAfile", "ca.crt", "-verify_hostname", dns, "-purpose", "sslserver", "tls.crt"],
    ]
    for command in commands:
        result = subprocess.run(["openssl", *command], cwd=output, capture_output=True, timeout=30)
        injector.require(result.returncode == 0, "tls_generation_failed")
    return tuple((output / name).read_bytes() for name in ("ca.crt", "tls.crt", "tls-private.key"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private directory; never a repository directory")
    parser.add_argument("--server-image", default=SERVER_IMAGE, help="Approved pinned public Python base; avoids private-registry credentials")
    parser.add_argument("--runtime-image", required=True, help="Exact unchanged GROMACS image digest")
    parser.add_argument("--instance", choices=["recovery", "init", "synthetic"], default="recovery",
                        help="Separate exact task-owned server/webhook identities; never updates an existing instance")
    parser.add_argument("--shard", default="window-01")
    parser.add_argument("--pause-seconds", type=int, default=0, help="0 disables; optional second-attempt pause 150..600")
    parser.add_argument("--synthetic-no-eligible-capacity", action="store_true", help="Separate synthetic eligibility test; never fleet exhaustion")
    parser.add_argument("--apiserver-source-cidr", action="append", default=[])
    args = parser.parse_args()
    os.umask(0o077)
    output = args.output.resolve()
    injector.require(not output.is_relative_to(Path(__file__).resolve().parents[3]), "private_tls_must_not_enter_worktree")
    config = {"tenant": injector.TENANT, "namespace": injector.NAMESPACE, "dead_pool": "h100-1x", "shard": args.shard,
              "runtime_image": args.runtime_image, "pause_seconds": args.pause_seconds,
              "synthetic_no_capacity": args.synthetic_no_eligible_capacity,
              "expires_at": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()}
    injector.validate_config(config)
    if args.instance == "init":
        injector.require(args.shard == "window-03" and args.pause_seconds >= 150 and not args.synthetic_no_eligible_capacity,
                         "init_instance_requires_disjoint_window03_pause")
    if args.instance == "synthetic":
        injector.require(args.shard == "window-01" and args.synthetic_no_eligible_capacity and args.pause_seconds == 0,
                         "synthetic_instance_requires_disjoint_window01_wait")
    injector.require(args.server_image == SERVER_IMAGE, "server_image_not_approved_base")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    ca, cert, key = certificates(output, args.instance)
    source = Path(__file__).with_name("admission_injector.py").read_text()
    resources = bundle(config, args.server_image, source, ca, cert, key, args.apiserver_source_cidr, args.instance)
    for filename, value in resources.items():
        (output / filename).write_text(json.dumps(value, indent=2) + "\n")
    plan = {"schema": "admitted-pool-recovery-injector/v1", "state": "rendered-not-deployed", "config": config,
            "server_image": args.server_image, "name": instance_identity(args.instance)[0],
            "namespace": instance_identity(args.instance)[1], "instance": args.instance,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "resource_sha256": {name: digest(value) for name, value in resources.items() if "private" not in name},
            "cleanup_order": ["webhook.json", "server.json", "tls-secret-private.json", "namespace.json"],
            "notes": ["No cluster writes performed", "No node, quota, queue, model, or status mutation",
                      "Injected init pause is not evidence of real scale-from-zero", "Expire/disable injector before unrelated tenant reuse"]}
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps({"state": plan["state"], "plan": str(output / "plan.json"), "expires_at": config["expires_at"]}))


if __name__ == "__main__":
    main()
