"""Real internal-job finalization fault: completed MD, failed ack, CPU-only retry.

No runtime patch or artificial scientific result. A directory occupying this
owned run's RESULT.json prevents its final atomic acknowledgement. After exit
75, a bounded CPU helper removes only that empty directory from retained scratch.
The public REST/MCP retry must deliver the original result without rerunning MD.
"""

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pilot


def kubectl(context, *args):
    return pilot.command("kubectl", "--context", context, "-n", pilot.NAMESPACE, *args)


def run(directory, name):
    fault_path = directory / (name + "-delivery-fault.json")
    if fault_path.exists():
        # Resume observation after operator interruption, never rerun the MD.
        evidence = json.loads(fault_path.read_bytes())
        current = pilot.status(directory, name)
        assert evidence["public_operation_id"] == current["operation_id"]
        assert not evidence.get("fault_removed"), "repair already ran; inspect its retry receipt"
        pod_name = evidence["compute_pod"]
    else:
        current = pilot.wait_for(directory, name, ready=True)
        pilot.identity_boundaries(directory, name)
        pod_name = current["attempt_id"]
        context = pilot.CONTEXTS[current["cluster"]]
        pod = json.loads(kubectl(context, "get", "pod", pod_name, "-o", "json"))
        claim = next(
            v["persistentVolumeClaim"]["claimName"] for v in pod["spec"]["volumes"] if v["name"] == "work"
        )
        main = next(c for c in pod["spec"]["containers"] if c["name"] == "main")
        evidence = {
            "at": pilot.now(),
            "public_operation_id": current["operation_id"],
            "compute_pod": pod_name,
            "compute_pod_uid": pod["metadata"]["uid"],
            "cluster": current["cluster"],
            "pvc": claim,
            "image": main["image"],
            "node": pod["spec"]["nodeName"],
            "fault": "owned RESULT.json path cannot be atomically replaced",
        }
        if not pod_name.startswith(current["serverless_operation_id"] + "-"):
            raise ValueError("refusing a pod outside the owned internal operation")
        kubectl(
            context,
            "exec",
            pod_name,
            "-c",
            "main",
            "--",
            "/opt/scientific-ai/bin/python",
            "-c",
            "from pathlib import Path; assert not Path('/work/DELIVERY.json').exists(); "
            "Path('/work/RESULT.json').mkdir()",
        )
        pilot.write(fault_path, evidence)
    context = pilot.CONTEXTS[evidence["cluster"]]
    claim = evidence["pvc"]
    assert "@sha256:" in evidence["image"]
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        state = pilot.status(directory, name)
        if state["status"] == "FAILED":
            break
        if state["status"] in {"CANCELLED", "SUCCEEDED", "TIMED_OUT", "EXPORT_FAILED"}:
            raise AssertionError("delivery fault did not produce the expected computation exit 75")
        time.sleep(10)
    else:
        raise TimeoutError("delivery fault did not reach a terminal result")
    # MultiKueue legitimately deletes completed worker Pods before polling.
    # The authenticated durable attempt record must outlive that cleanup.
    ended = next(a for a in state["attempts"] if a["attempt_id"] == pod_name)
    exit_code = ended["exit_code"]
    assert exit_code == 75, "not a successful computation with pending delivery"
    evidence["compute_exit_code"] = exit_code

    # No GPU requested. Normal Kueue CPU admission mounts the retained regional
    # RWO volume; the actual platform finalizer remains scheduled by Serverless.
    helper_name = "delivery-repair-" + current["operation_id"][:12]
    helper_script = """import hashlib,json
from pathlib import Path
work=Path('/work')
marker=json.loads((work/'DELIVERY.json').read_bytes())
assert marker['compute_attempt']['attempt_id']==EXPECTED_POD
result=json.loads((work/marker['workspace']/'result.json').read_bytes())
digest=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
assert digest==marker['result_sha256'] and result['status']=='succeeded'
(work/'RESULT.json').rmdir()
print(json.dumps({'original_result_sha256':digest,'original_commands':len(result['commands']),'fault_removed':True}))
""".replace("EXPECTED_POD", repr(pod_name))
    helper = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": helper_name,
            "namespace": pilot.NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "scientific-ai-acceptance",
                "kueue.x-k8s.io/queue-name": "default",
            },
            "annotations": {
                "purpose": "internal delivery fault repair",
                "owner": "fs2-new-fleet-gromacs-acceptance-r20261012",
                "expiry": "delete after this bounded test",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 180,
            "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "repair",
                    "image": evidence["image"],
                    "command": ["/opt/scientific-ai/bin/python", "-c", helper_script],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "64Mi"},
                        "limits": {"cpu": "500m", "memory": "128Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                }
            ],
            "volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": claim}}],
        },
    }
    manifest = directory / (name + "-repair-pod.json")
    pilot.write(manifest, helper)
    kubectl(context, "create", "--dry-run=server", "-f", str(manifest))
    kubectl(context, "create", "-f", str(manifest))
    kubectl(
        context, "wait", "--for=jsonpath={.status.phase}=Succeeded", "pod/" + helper_name, "--timeout=180s"
    )
    evidence.update(json.loads(kubectl(context, "logs", helper_name)))
    kubectl(context, "delete", "pod", helper_name, "--wait=true", "--timeout=60s")
    pilot.write(directory / (name + "-delivery-fault.json"), evidence)

    with httpx.Client(
        base_url=pilot.PUBLIC, headers={"Authorization": "Bearer " + pilot.keys()["worker"]}, timeout=120
    ) as client:

        def retry(path):
            op = current["operation_id"]
            if path == "mcp":
                return pilot.call_tool(
                    client,
                    "retry_scientific_delivery",
                    {"operation_id": op, "idempotency_key": name + "-delivery"},
                )
            response = client.post(
                "/v1/operations/" + op + ":export", headers={"Idempotency-Key": name + "-delivery"}
            )
            response.raise_for_status()
            return response.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(retry, ["rest", "mcp"]))
        assert all(r["operation_id"] == current["operation_id"] for r in replies)
        evidence["rest_mcp_retries_same_public_handle"] = True
    completed = pilot.wait_for(directory, name, "mcp")
    finalizers = [a for a in completed["attempts"] if (a.get("role") or a.get("attempt_role")) == "finalize"]
    assert len(finalizers) == 1, "concurrent retries must create exactly one finalizer attempt"
    assert not finalizers[0].get("gpu_class")
    finalizer_job_name = finalizers[0]["attempt_id"].rsplit("-", 1)[0]
    finalizer_job = json.loads(kubectl(pilot.CONTEXT, "get", "job", finalizer_job_name, "-o", "json"))
    finalizer_spec = finalizer_job["spec"]["template"]["spec"]
    gpu_requests = sum(
        int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
        for c in finalizer_spec["containers"] + finalizer_spec.get("initContainers", [])
    )
    assert gpu_requests == 0, "delivery retry must request no GPUs"
    node = json.loads(kubectl(context, "get", "node", finalizers[0]["node"], "-o", "json"))
    node_pool = node["metadata"]["labels"].get("serverless2.nebius/pool")
    node_gpus = int(node["status"]["allocatable"].get("nvidia.com/gpu", 0))
    assert node_pool == "system" and node_gpus == 0, (
        "zero-GPU requests are insufficient: delivery must not depend on GPU-pool nodes"
    )
    compute_attempts = [a for a in completed["attempts"] if a.get("attempt_role", "run") == "run"]
    assert len(compute_attempts) == 1, "delivery-pending must not restart the GPU attempt"
    evidence["finalizer"] = {
        key: finalizers[0].get(key)
        for key in ("attempt_id", "epoch", "attempt_role", "gpu_class", "exit_code")
    }
    evidence["finalizer"]["requested_gpus"] = gpu_requests
    evidence["finalizer"].update(node=finalizers[0]["node"], node_pool=node_pool, node_gpus=node_gpus)
    result = json.loads((directory / (name + "-result.json")).read_bytes())["native_result"]
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == evidence["original_result_sha256"], "delivery retry changed the scientific result"
    assert len(result["commands"]) == evidence["original_commands"], (
        "delivery retry executed new scientific commands"
    )
    evidence.update(completed_at=pilot.now(), exact_original_result_delivered=True)
    pilot.write(directory / (name + "-delivery-fault.json"), evidence)
    print(json.dumps(evidence))


if __name__ == "__main__":
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    pilot.configure(args.deployment)
    run(args.private_dir, args.name)
