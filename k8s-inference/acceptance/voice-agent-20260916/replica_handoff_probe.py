"""Real API-server SSA semantics on a disposable paused, zero-Pod Deployment.

Requires an operator-owned loopback kubectl proxy. Creates only named temporary
Deployment/Lease fixtures, never GPUs or a real HPA, and deletes both in finally.
"""

import argparse
import asyncio
import copy
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from fs2_serve.model_deployment import FIELD_MANAGER, RenderedResource, canonical_digest
from fs2_serve.model_deployment_controller import HttpKubernetesModelClient, LeaseFence


async def run(args):
    name = "fs2-voice-replica-probe-" + uuid4().hex[:8]
    command = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "-n",
        "fs2-models",
    ]

    def kubectl(*tail, body=None):
        return subprocess.check_output(
            command + list(tail), input=json.dumps(body).encode() if body else None
        )

    parent = json.loads(
        kubectl("get", "modeldeployment", "parakeet-realtime-eou-120m-v1", "-o", "json")
    )
    owner = {
        "apiVersion": parent["apiVersion"],
        "kind": "ModelDeployment",
        "name": parent["metadata"]["name"],
        "uid": parent["metadata"]["uid"],
        "controller": True,
    }
    now, epoch = datetime.now(UTC), uuid4().hex
    lease = {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": name,
            "namespace": "fs2-models",
            "annotations": {"inference.fs2.nebius.ai/fence-token": epoch},
        },
        "spec": {
            "holderIdentity": name,
            "leaseDurationSeconds": 120,
            "renewTime": now.isoformat(),
        },
    }
    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": "fs2-models",
            "ownerReferences": [owner],
            "labels": {"workload.fs2.nebius/owner": "voice-replica-probe-20260916"},
        },
        "spec": {
            "paused": True,
            "replicas": 0,
            "selector": {"matchLabels": {"voice-probe": name}},
            "template": {
                "metadata": {"labels": {"voice-probe": name}},
                "spec": {
                    "containers": [
                        {"name": "never-started", "image": "registry.k8s.io/pause:3.10"}
                    ]
                },
            },
        },
    }
    receipt = {
        "fixture": name,
        "scope": "paused Deployment; no scheduled Pods or GPUs",
        "cleaned_up": False,
    }
    try:
        kubectl("create", "-f", "-", body=lease)
        kubectl(
            "apply",
            "--server-side",
            "--field-manager=" + FIELD_MANAGER,
            "-f",
            "-",
            body=manifest,
        )
        kubectl(
            "patch",
            "deployment",
            name,
            "--subresource=scale",
            "--type=merge",
            "--field-manager=keda",
            "-p",
            '{"spec":{"replicas":1}}',
        )
        desired = copy.deepcopy(manifest)
        desired["spec"]["replicas"] = 2
        resource = RenderedResource(
            api_version="apps/v1",
            kind="Deployment",
            namespace="fs2-models",
            name=name,
            manifest=desired,
            digest=canonical_digest(desired),
        )
        lease_body = json.loads(kubectl("get", "lease", name, "-o", "json"))
        fence = LeaseFence(
            namespace="fs2-models",
            name=name,
            holder_identity=name,
            token=epoch,
            resource_version=lease_body["metadata"]["resourceVersion"],
            renew_time=now,
            duration_seconds=120,
        )
        with tempfile.TemporaryDirectory(prefix="voice-ssa-probe-") as directory:
            # A kubectl proxy performs the actual kubeconfig authentication.
            # This local fixture satisfies the client shape, not an API credential.
            token = Path(directory) / "proxy-placeholder"
            token.write_text("local-kubectl-proxy-placeholder")

            async def proxy_auth(request):
                # Let the authenticated loopback proxy use its kubeconfig.
                request.headers.pop("authorization", None)

            async with httpx.AsyncClient(
                base_url=args.proxy,
                trust_env=False,
                event_hooks={"request": [proxy_auth]},
            ) as http:
                client = HttpKubernetesModelClient(
                    base_url=args.proxy,
                    token_file=token,
                    ca_file=token,
                    writes_enabled=True,
                    client=http,
                )
                fixed = await client.apply_resource(
                    resource, owner_uid=owner["uid"], fence=fence
                )
                receipt["fixed"] = {
                    "replicas": fixed.desired_replicas,
                    "managers": fixed.replica_field_managers,
                }
                assert fixed.desired_replicas == 2 and fixed.replica_field_managers == [
                    FIELD_MANAGER
                ]
                # Model the existing HPA taking its lower floor before the
                # controller relinquishes replicas in the reverse transition.
                kubectl(
                    "patch",
                    "deployment",
                    name,
                    "--subresource=scale",
                    "--type=merge",
                    "--field-manager=keda",
                    "-p",
                    '{"spec":{"replicas":1}}',
                )
                desired["spec"].pop("replicas")
                resource = resource.model_copy(
                    update={"manifest": desired, "digest": canonical_digest(desired)}
                )
                reverse = await client.apply_resource(
                    resource, owner_uid=owner["uid"], fence=fence
                )
                receipt["autoscaled"] = {
                    "replicas": reverse.desired_replicas,
                    "managers": reverse.replica_field_managers,
                }
                assert (
                    reverse.desired_replicas == 1
                    and FIELD_MANAGER not in reverse.replica_field_managers
                )
        pods = json.loads(
            kubectl("get", "pods", "-l", "voice-probe=" + name, "-o", "json")
        )
        assert not pods["items"]
        receipt.update(status="passed", pods_created=0)
    finally:
        kubectl("delete", "deployment", name, "--ignore-not-found=true", "--wait=false")
        kubectl("delete", "lease", name, "--ignore-not-found=true", "--wait=false")
        receipt["cleaned_up"] = True
        Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("kubeconfig", "context", "proxy", "output"):
        parser.add_argument("--" + key, required=True)
    asyncio.run(run(parser.parse_args()))
