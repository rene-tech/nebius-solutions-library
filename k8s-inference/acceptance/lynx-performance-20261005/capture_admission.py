"""Read-only, exact-operation Kueue/Pod allocation proof while resources exist."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from recipes import save, sha

PREFIX = "fs2.nebius.ai/"
GPU = "nvidia.com/gpu"
SHAPE_ENV = {"FS2_GROMACS_MPI_TRANSPORT", "FS2_GROMACS_MPI_NODES", "FS2_GROMACS_MPI_GPUS_PER_NODE",
             "FS2_GROMACS_MPI_RANKS_PER_NODE", "FS2_GROMACS_MPI_TOTAL_RANKS"}


def resources(containers):
    return [{"name": c["name"], "image": c.get("image"), "resources": c.get("resources", {}),
             "security_context": c.get("securityContext", {}),
             "shape_environment": {entry["name"]: entry["value"] for entry in c.get("env", [])
                                   if entry["name"] in SHAPE_ENV and "value" in entry}}
            for c in containers]


def allocation(plan, pods, workloads, *, nodes=()):
    stages = plan["plan"]["stages"]
    if len(stages) != 1 or plan["tenant_id"] != "system" or plan["model_id"] not in ("gromacs", "gromacs-mpi"):
        raise ValueError("Require one-stage internal MD plan")
    stage = stages[0]
    per_pod = stage.get("execution_shape", {}).get("accelerator_count", 1)
    if stage["mode"] not in ("gang-jobset", "independent-jobs"):
        raise ValueError("Unknown frozen stage mode")
    count = stage["gang_size"] if stage["mode"] == "gang-jobset" else 1
    operation = plan["operation_id"]
    projected = []
    for p in pods:
        labels = p["metadata"].get("labels", {})
        if labels.get(PREFIX + "operation-id") != operation or labels.get(PREFIX + "tenant-id") != "system":
            raise ValueError("Refuse unrelated Pod evidence")
        projected.append({"name": p["metadata"]["name"], "uid": p["metadata"]["uid"],
                          "node": p["spec"].get("nodeName"), "phase": p["status"].get("phase"),
                          "node_selector": p["spec"].get("nodeSelector", {}),
                          "pod_security_context": p["spec"].get("securityContext", {}),
                          "labels": labels, "containers": resources(p["spec"]["containers"]),
                          "container_statuses": p["status"].get("containerStatuses", [])})
    admitted = []
    for w in workloads:
        sets = [{"name": s["name"], "count": s["count"],
                 "containers": resources(s["template"]["spec"]["containers"])} for s in w["spec"]["podSets"]]
        admitted.append({"name": w["metadata"]["name"], "uid": w["metadata"]["uid"],
                         "pod_sets": sets, "admission": w.get("status", {}).get("admission"),
                         "conditions": w.get("status", {}).get("conditions", [])})
    pod_counts = [sum(int(c.get("resources", {}).get("requests", {}).get(GPU, 0)) for c in p["containers"])
                  for p in projected]
    kueue_counts = [sum(int(a.get("resourceUsage", {}).get(GPU, 0))
                       for a in (w["admission"] or {}).get("podSetAssignments", [])) for w in admitted]
    result = {"operation_id": operation, "shape_id": stage.get("execution_shape", {}).get("shape_id"),
            "expected_pod_count": count, "expected_gpus_per_pod": per_pod, "expected_total_gpus": count * per_pod,
            "pods": projected, "kueue_workloads": admitted,
            "observed_pod_gpu_requests": pod_counts, "kueue_admitted_total_gpu_counts": kueue_counts,
            "pods_match_frozen_shape": len(pod_counts) == count and all(n == per_pod for n in pod_counts),
            "kueue_matches_frozen_shape": kueue_counts == [count * per_pod],
            "scope": "Point-in-time reservation proof, not a GPU-time integral or native utilization measurement."}
    rdma = stage.get("execution_shape", {}).get("rdma")
    if rdma is not None:
        resource, amount = rdma["resource_name"], rdma["count"]
        labels = {"topology.fs2.nebius/scope": "gpu_cluster",
                  "topology.nebius.com/gpu-cluster-id": rdma["gpu_cluster_id"]}
        hca_counts = [sum(int(c["resources"].get("requests", {}).get(resource, 0)) for c in p["containers"])
                      for p in projected]
        hca_limits = [sum(int(c["resources"].get("limits", {}).get(resource, 0)) for c in p["containers"])
                      for p in projected]
        hca_quota = [sum(int(a.get("resourceUsage", {}).get(resource, 0))
                        for a in (w["admission"] or {}).get("podSetAssignments", [])) for w in admitted]
        assignments = [a for w in admitted for a in (w["admission"] or {}).get("podSetAssignments", [])]
        joint = bool(assignments) and all(a.get("flavors", {}).get(GPU) is not None
            and a["flavors"][GPU] == a.get("flavors", {}).get(resource) for a in assignments)
        sets = [s for w in admitted for s in w["pod_sets"]]
        sets_match = bool(sets) and sum(s["count"] for s in sets) == count and all(
            sum(int(c["resources"].get("requests", {}).get(resource, 0)) for c in s["containers"]) == amount
            and sum(int(c["resources"].get("requests", {}).get(GPU, 0)) for c in s["containers"]) == per_pod
            for s in sets)
        node_facts = [{"name": n["metadata"]["name"],
                       "labels": {k: v for k, v in n["metadata"].get("labels", {}).items()
                                  if k in {*labels, "accelerator.fs2.nebius/pool-id"}},
                       "allocatable": {k: v for k, v in n["status"].get("allocatable", {}).items()
                                       if k in {GPU, resource}}} for n in nodes]
        node_match = (len(node_facts) == count
                      and {n["name"] for n in node_facts} == {p["node"] for p in projected}
                      and all(all(n["labels"].get(k) == v for k, v in labels.items())
                              and int(n["allocatable"].get(resource, 0)) >= amount
                              and int(n["allocatable"].get(GPU, 0)) >= per_pod for n in node_facts))
        pod_match = (len(projected) == count and hca_counts == [amount] * count
                     and hca_limits == [amount] * count
                     and len({p["node"] for p in projected}) == count
                     and all(all(p["node_selector"].get(k) == v for k, v in labels.items()) for p in projected))
        result["rdma"] = {"frozen_binding": rdma, "expected_total_bundles": count * amount,
                          "pod_bundle_requests": hca_counts, "pod_bundle_limits": hca_limits,
                          "kueue_total_bundle_counts": hca_quota, "joint_gpu_rdma_flavor": joint,
                          "pod_sets_match_frozen_binding": sets_match,
                          "pods_match_frozen_binding": pod_match, "node_facts": node_facts,
                          "nodes_match_frozen_binding": node_match,
                          "kueue_matches_frozen_binding": joint and sets_match and hca_quota == [count * amount]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Never overwrite a previous allocation observation")
    plan = json.loads(args.plan.read_text())
    operation = str(UUID(plan["operation_id"]))
    kube = ["kubectl", "--context", args.context, "--request-timeout=20s", "-n", "fs2-models"]

    def get(kind, selector):
        return json.loads(subprocess.check_output([*kube, "get", kind, "-l", selector, "-o", "json"], timeout=25))["items"]

    selector = PREFIX + "operation-id=" + operation
    pods = get("pods", selector)
    owners = get("jobs.batch", selector) + get("jobsets.jobset.x-k8s.io", selector)
    workloads = {}
    for owner in owners:
        if owner["metadata"].get("labels", {}).get(PREFIX + "tenant-id") != "system":
            raise ValueError("Refuse unrelated workload owner")
        for workload in get("workloads.kueue.x-k8s.io", "kueue.x-k8s.io/job-uid=" + owner["metadata"]["uid"]):
            workloads[workload["metadata"]["uid"]] = workload
    nodes = []
    if plan["plan"]["stages"][0].get("execution_shape", {}).get("rdma"):
        for name in sorted({p["spec"].get("nodeName") for p in pods} - {None}):
            nodes.append(json.loads(subprocess.check_output(
                [*kube, "get", "node", name, "-o", "json"], timeout=25)))
    proof = allocation(plan, pods, list(workloads.values()), nodes=nodes)
    save(args.output, {**proof, "captured_at": datetime.now(timezone.utc).isoformat(),
                       "plan_file": str(args.plan.resolve()), "plan_sha256": sha(args.plan),
                       "capture_source_sha256": sha(Path(__file__))})
    print(json.dumps({k: v for k, v in proof.items() if k not in ("pods", "kueue_workloads")}))


if __name__ == "__main__":
    main()
