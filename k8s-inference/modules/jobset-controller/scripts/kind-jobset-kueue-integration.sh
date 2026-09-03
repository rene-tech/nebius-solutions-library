#!/usr/bin/env bash
# Prove the JobSet/Kueue runtime path on a pinned local Kind cluster.
#
# Kueue v0.17.8 is compiled against a different sigs.k8s.io/jobset minor than
# the JobSet v0.12.0 release this module installs, so listing the JobSet
# integration in the Kueue configuration is not evidence that the pair works.
# This installs both in the foundation's order (JobSet CRD and controller
# first, then Kueue), submits a real v1alpha2 JobSet carrying the Kueue queue
# label, and proves Kueue creates, owns, and admits its Workload with the exact
# true-gang PodSet shape.
#
# It creates and deletes its own Kind cluster and never touches a cloud
# project, a shared cluster, or any live queue.
set -euo pipefail

kueue_version="v0.17.8"
kueue_chart_ref="oci://registry.k8s.io/kueue/charts/kueue"
kueue_chart_digest="sha256:e5f000fcf0604e5dea0025e0ffdd20e6712de432bcca0ec254d71d97f012a354"
kueue_chart_archive_sha256="409de6260d2b7834fece5044502822bcb4e74ed8a03b8ea22bb78bcdfa1627db"
jobset_version="v0.12.0"
jobset_chart_ref="oci://registry.k8s.io/jobset/charts/jobset"
jobset_chart_digest="sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24"
jobset_chart_archive_sha256="bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a"
jobset_image="registry.k8s.io/jobset/jobset:v0.12.0@sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d"
# Kubernetes 1.34 is inside both upstream end-to-end matrices: JobSet v0.12.0
# covers 1.32-1.34 and Kueue v0.17.8 covers 1.33-1.35.
kind_node_image="kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a"
cluster_name="fs2-jobset-kueue-$RANDOM"
work_dir="$(mktemp -d /tmp/fs2-jobset-kueue.XXXXXX)"
kubeconfig="${work_dir}/kubeconfig"
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
production_values="${repository_root}/stages/foundation/values/kueue.yaml"

cleanup() {
  kind delete cluster --name "${cluster_name}" >/dev/null 2>&1 || true
  find "${work_dir}" -type f -delete
  find "${work_dir}" -depth -type d -empty -delete
}
trap cleanup EXIT

helm pull "${jobset_chart_ref}@${jobset_chart_digest}" --destination "${work_dir}" >/dev/null
jobset_chart="$(find "${work_dir}" -maxdepth 1 -type f -name 'jobset*.tgz' -print -quit)"
[[ "$(sha256sum "${jobset_chart}" | awk '{print $1}')" == "${jobset_chart_archive_sha256}" ]]
helm pull "${kueue_chart_ref}@${kueue_chart_digest}" --destination "${work_dir}" >/dev/null
kueue_chart="$(find "${work_dir}" -maxdepth 1 -type f -name 'kueue*.tgz' -print -quit)"
[[ "$(sha256sum "${kueue_chart}" | awk '{print $1}')" == "${kueue_chart_archive_sha256}" ]]

kind create cluster \
  --name "${cluster_name}" \
  --image "${kind_node_image}" \
  --kubeconfig "${kubeconfig}" \
  --wait 180s >/dev/null
kubectl=(kubectl --kubeconfig "${kubeconfig}")
"${kubectl[@]}" label nodes --all workload.fs2.nebius/system=true --overwrite >/dev/null
server_version="$("${kubectl[@]}" version -o json | jq -r '.serverVersion.gitVersion')"
[[ "$(sed -E 's/^v?([0-9]+\.[0-9]+).*/v\1/' <<<"${server_version}")" == "v1.34" ]]

# Foundation order: the JobSet CRD and controller exist before Kueue starts, so
# Kueue's JobSet integration has an API to reconcile against.
"${kubectl[@]}" create namespace jobset-system >/dev/null
helm show crds "${jobset_chart}" >"${work_dir}/jobset-crds.yaml"
"${kubectl[@]}" apply --server-side --force-conflicts \
  --field-manager=fs2-jobset-crd -f "${work_dir}/jobset-crds.yaml" >/dev/null
"${kubectl[@]}" wait --for=condition=Established --timeout=180s \
  crd/jobsets.jobset.x-k8s.io >/dev/null
helm install fs2-jobset "${jobset_chart}" \
  --namespace jobset-system \
  --kubeconfig "${kubeconfig}" \
  --skip-crds \
  --set image.repository="${jobset_image%%:*}" \
  --set-string image.tag="${jobset_image#*:}" \
  --set-string 'controller.nodeSelector.workload\.fs2\.nebius/system=true' \
  --wait --timeout 300s >/dev/null

helm install kueue "${kueue_chart}" \
  --namespace kueue-system \
  --create-namespace \
  --kubeconfig "${kubeconfig}" \
  --values "${production_values}" \
  --wait --timeout 300s >/dev/null
"${kubectl[@]}" -n kueue-system rollout status deployment/kueue-controller-manager --timeout=300s >/dev/null

# Two namespaces on one ClusterQueue, exactly as the scheduling module renders.
"${kubectl[@]}" create namespace fs2-models >/dev/null
"${kubectl[@]}" create namespace fs2-academic-poc >/dev/null
"${kubectl[@]}" apply --server-side -f - >/dev/null <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: example-regular
spec:
  nodeLabels:
    workload.fs2.nebius/system: "true"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: inference-accelerators
spec:
  namespaceSelector:
    matchExpressions:
      - key: kubernetes.io/metadata.name
        operator: In
        values: [fs2-academic-poc, fs2-models]
  queueingStrategy: BestEffortFIFO
  admissionScope:
    admissionMode: UsageBasedAdmissionFairSharing
  preemption:
    reclaimWithinCohort: LowerPriority
    withinClusterQueue: LowerPriority
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "8"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: academic-scientific
  namespace: fs2-academic-poc
spec:
  clusterQueue: inference-accelerators
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: presentation
value: 1000
description: FS2 presentation workload priority
YAML
"${kubectl[@]}" wait --for=condition=Active --timeout=120s \
  clusterqueue/inference-accelerators >/dev/null

# A real true-gang JobSet in the licensed-asset namespace, carrying the Kueue
# queue and priority labels the controller-owned writer projects.
"${kubectl[@]}" apply --server-side -f - >/dev/null <<'YAML'
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: fs2-gang-probe
  namespace: fs2-academic-poc
  labels:
    kueue.x-k8s.io/queue-name: academic-scientific
    kueue.x-k8s.io/priority-class: presentation
spec:
  suspend: true
  failurePolicy:
    maxRestarts: 0
  replicatedJobs:
    - name: gang
      replicas: 2
      template:
        spec:
          parallelism: 1
          completions: 1
          backoffLimit: 0
          template:
            metadata:
              labels:
                fs2.nebius.ai/model-id: alphafold3
                fs2.nebius.ai/tenant-id: tenant-academic
                fs2.nebius.ai/service-class: presentation
            spec:
              restartPolicy: Never
              containers:
                - name: worker
                  image: registry.k8s.io/pause:3.10
                  resources:
                    requests:
                      example.com/accelerator: "1"
                    limits:
                      example.com/accelerator: "1"
YAML

workload=""
for _ in $(seq 1 60); do
  workload="$("${kubectl[@]}" -n fs2-academic-poc get workloads \
    -o jsonpath='{range .items[?(@.metadata.ownerReferences[0].kind=="JobSet")]}{.metadata.name}{"\n"}{end}' 2>/dev/null | head -1)"
  [[ -n "${workload}" ]] && break
  sleep 2
done
[[ -n "${workload}" ]] || { echo "Kueue created no Workload for the JobSet" >&2; exit 1; }

owner_kind="$("${kubectl[@]}" -n fs2-academic-poc get "workload/${workload}" \
  -o jsonpath='{.metadata.ownerReferences[0].kind}')"
owner_name="$("${kubectl[@]}" -n fs2-academic-poc get "workload/${workload}" \
  -o jsonpath='{.metadata.ownerReferences[0].name}')"
[[ "${owner_kind}" == "JobSet" && "${owner_name}" == "fs2-gang-probe" ]]

for _ in $(seq 1 60); do
  admitted="$("${kubectl[@]}" -n fs2-academic-poc get "workload/${workload}" \
    -o jsonpath='{.status.conditions[?(@.type=="Admitted")].status}' 2>/dev/null || true)"
  [[ "${admitted}" == "True" ]] && break
  sleep 2
done
[[ "${admitted}" == "True" ]] || {
  echo "Kueue did not admit the JobSet Workload" >&2
  "${kubectl[@]}" -n fs2-academic-poc get "workload/${workload}" -o yaml >&2
  exit 1
}

# The true-gang shape must survive: one PodSet per replicated Job, each with the
# Job's parallelism, and the accelerator assigned from the reviewed flavor.
"${kubectl[@]}" -n fs2-academic-poc get "workload/${workload}" -o json >"${work_dir}/workload.json"
python3 - "${work_dir}/workload.json" <<'PY'
import json
import sys

workload = json.load(open(sys.argv[1]))
# Observed contract, Kueue v0.17.8 + JobSet v0.12.0: one PodSet per
# replicatedJob NAME, whose count is replicas x parallelism, and whose
# topologyRequest.subGroupCount records the replicated-job gang grouping. A
# consumer must read the gang this way instead of expecting one PodSet per
# replica.
pod_sets = workload["spec"]["podSets"]
assert len(pod_sets) == 1, pod_sets
gang = pod_sets[0]
assert gang["name"] == "gang", gang
# replicas x nested parallelism, which the production renderer keeps at 1.
assert gang["count"] == 2, gang
topology = gang["topologyRequest"]
assert topology["subGroupCount"] == 2, topology
assert topology["subGroupIndexLabel"] == "jobset.sigs.k8s.io/job-index", topology
requests = gang["template"]["spec"]["containers"][0]["resources"]["requests"]
assert requests["example.com/accelerator"] == "1", requests
admission = workload["status"]["admission"]
assert admission["clusterQueue"] == "inference-accelerators", admission
assignments = admission["podSetAssignments"]
assert len(assignments) == 1, assignments
assignment = assignments[0]
assert assignment["flavors"]["example.com/accelerator"] == "example-regular", assignment
assert int(assignment["resourceUsage"]["example.com/accelerator"]) == 2, assignment
# QuotaReserved and Admitted are separate transitions and must both be present
# and independently readable, because a consumer records them separately.
conditions = {item["type"]: item for item in workload["status"]["conditions"]}
reserved = [conditions["QuotaReserved"]] if "QuotaReserved" in conditions else []
assert reserved and reserved[0]["status"] == "True", workload["status"]["conditions"]
admitted_condition = conditions.get("Admitted")
assert admitted_condition is not None and admitted_condition["status"] == "True", conditions
assert reserved[0]["lastTransitionTime"] <= admitted_condition["lastTransitionTime"], conditions
print(
    "podsets=%d gang_count=%d subgroups=%d admitted_queue=%s accelerators=%d reserved_at=%s"
    % (
        len(pod_sets),
        gang["count"],
        topology["subGroupCount"],
        admission["clusterQueue"],
        int(assignment["resourceUsage"]["example.com/accelerator"]),
        reserved[0]["lastTransitionTime"],
    )
)
print("admitted_at=%s" % admitted_condition["lastTransitionTime"])
PY

# Kueue must also release the JobSet to run once it is admitted.
for _ in $(seq 1 30); do
  suspended="$("${kubectl[@]}" -n fs2-academic-poc get jobset/fs2-gang-probe \
    -o jsonpath='{.spec.suspend}' 2>/dev/null || true)"
  [[ "${suspended}" == "false" ]] && break
  sleep 2
done
[[ "${suspended}" == "false" ]] || { echo "Kueue did not unsuspend the admitted JobSet" >&2; exit 1; }

# Deleting the JobSet must garbage-collect its owned Workload. Wait for that
# rather than sampling immediately, so ownership is actually proved.
"${kubectl[@]}" -n fs2-academic-poc delete jobset/fs2-gang-probe --wait=true >/dev/null
remaining=1
for _ in $(seq 1 30); do
  remaining="$("${kubectl[@]}" -n fs2-academic-poc get workloads -o name 2>/dev/null | wc -l)"
  [[ "${remaining}" -eq 0 ]] && break
  sleep 2
done
if [[ "${remaining}" -ne 0 ]]; then
  echo "the JobSet's Workload was not garbage-collected: ${remaining} remaining" >&2
  "${kubectl[@]}" -n fs2-academic-poc get workloads -o yaml >&2
  exit 1
fi

printf 'JobSet/Kueue integration proved server=%s jobset=%s kueue=%s workload=%s owner=JobSet admitted=true unsuspended=true residual_workloads=%s\n' \
  "${server_version}" "${jobset_version}" "${kueue_version}" "${workload}" "${remaining}"
