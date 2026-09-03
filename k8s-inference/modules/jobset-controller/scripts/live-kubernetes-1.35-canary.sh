#!/usr/bin/env bash
# Run two task-owned CPU JobSets against an existing Kueue installation.
#
# The completion canary proves a two-replica JobSet is admitted, unsuspended,
# scheduled, and completed. The cleanup canary is deleted while running and
# must leave no child Job, Pod, or Kueue Workload. The script never creates or
# changes a queue, ResourceFlavor, quota, namespace, node, or GPU workload.
set -euo pipefail

: "$FS2_JOBSET_KUBECONFIG"
: "$FS2_JOBSET_CONTEXT"
: "$FS2_JOBSET_CLUSTER_ID"
: "$FS2_JOBSET_SERVER_VERSION"
: "$FS2_JOBSET_NAMESPACE"
: "$FS2_JOBSET_QUEUE"
: "$FS2_JOBSET_CONTROLLER_NAMESPACE"
: "$FS2_JOBSET_CONTROLLER"
: "$FS2_KUEUE_CONTROLLER_NAMESPACE"
: "$FS2_KUEUE_CONTROLLER"
: "$FS2_JOBSET_CANARY_RUN_ID"

case "$FS2_JOBSET_KUBECONFIG" in
  /*) ;;
  *) echo "kubeconfig must be absolute" >&2; exit 2 ;;
esac
if [[ ! "$FS2_JOBSET_CANARY_RUN_ID" =~ ^[a-z][a-z0-9]{5,11}$ ]]; then
  echo "canary run ID must be 6-12 lowercase alphanumeric characters" >&2
  exit 2
fi

jobset_chart_version="0.12.0"
jobset_chart_digest="sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24"
jobset_chart_archive_sha256="bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a"
jobset_image="registry.k8s.io/jobset/jobset:v0.12.0@sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d"
jobset_image_digest="sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d"
kueue_version="0.17.8"
kueue_image_digest="sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6"
canary_image="registry.k8s.io/e2e-test-images/busybox:1.36.1-1@sha256:a9155b13325b2abef48e71de77bb8ac015412a566829f621d06bfae5c699b1b9"
complete_name="fs2-js135-$FS2_JOBSET_CANARY_RUN_ID-complete"
cleanup_name="fs2-js135-$FS2_JOBSET_CANARY_RUN_ID-cleanup"
work_dir="$(mktemp -d /tmp/fs2-jobset-live.XXXXXX)"

k() {
  kubectl --kubeconfig "$FS2_JOBSET_KUBECONFIG" --context "$FS2_JOBSET_CONTEXT" "$@"
}

cleanup_owned() {
  k -n "$FS2_JOBSET_NAMESPACE" delete jobset "$complete_name" "$cleanup_name" \
    --ignore-not-found --cascade=foreground --wait=false >/dev/null 2>&1 || true
  find "$work_dir" -type f -delete
  find "$work_dir" -depth -type d -empty -delete
}
trap cleanup_owned EXIT

actual_server="$(k version -o json | jq -r '.serverVersion.gitVersion')"
if [[ "$actual_server" != "$FS2_JOBSET_SERVER_VERSION" || "$actual_server" != v1.35.* ]]; then
  echo "expected exact Kubernetes $FS2_JOBSET_SERVER_VERSION in the v1.35 minor, got $actual_server" >&2
  exit 1
fi

k get crd jobsets.jobset.x-k8s.io -o json >"$work_dir/crd.json"
jq -e '
  any(.status.conditions[]; .type == "Established" and .status == "True") and
  any(.spec.versions[]; .name == "v1alpha2" and .served == true and .storage == true)
' "$work_dir/crd.json" >/dev/null

k -n "$FS2_JOBSET_CONTROLLER_NAMESPACE" get deployment "$FS2_JOBSET_CONTROLLER" -o json \
  >"$work_dir/jobset-controller.json"
jq -e --arg image "$jobset_image" '
  .status.availableReplicas == 1 and
  .spec.replicas == 1 and
  .spec.template.spec.containers[0].image == $image and
  .spec.template.spec.nodeSelector["workload.fs2.nebius/system"] == "true"
' "$work_dir/jobset-controller.json" >/dev/null
k -n "$FS2_JOBSET_CONTROLLER_NAMESPACE" get pods -o json >"$work_dir/jobset-controller-pods.json"
jq -e --arg digest "$jobset_image_digest" '
  any(.items[]; any(.status.containerStatuses[]?; .ready == true and (.imageID | endswith($digest))))
' "$work_dir/jobset-controller-pods.json" >/dev/null

k -n "$FS2_KUEUE_CONTROLLER_NAMESPACE" get deployment "$FS2_KUEUE_CONTROLLER" -o json \
  >"$work_dir/kueue-controller.json"
jq -e '.status.availableReplicas == 1 and .spec.replicas == 1' \
  "$work_dir/kueue-controller.json" >/dev/null
k -n "$FS2_KUEUE_CONTROLLER_NAMESPACE" get pods -o json >"$work_dir/kueue-controller-pods.json"
jq -e --arg digest "$kueue_image_digest" '
  any(.items[]; any(.status.containerStatuses[]?; .ready == true and (.imageID | endswith($digest))))
' "$work_dir/kueue-controller-pods.json" >/dev/null

k -n "$FS2_JOBSET_NAMESPACE" get localqueue.kueue.x-k8s.io "$FS2_JOBSET_QUEUE" -o json \
  >"$work_dir/localqueue.json"
jq -e 'any(.status.conditions[]; .type == "Active" and .status == "True")' \
  "$work_dir/localqueue.json" >/dev/null
cluster_queue="$(jq -r '.spec.clusterQueue' "$work_dir/localqueue.json")"
k get clusterqueue.kueue.x-k8s.io "$cluster_queue" -o json >"$work_dir/clusterqueue.json"
jq -e 'any(.status.conditions[]; .type == "Active" and .status == "True")' \
  "$work_dir/clusterqueue.json" >/dev/null

queue_spec_before="$(k get clusterqueues.kueue.x-k8s.io -o json |
  jq -S -c '[.items[] | {name: .metadata.name, spec: .spec}] | sort_by(.name)')"
flavor_spec_before="$(k get resourceflavors.kueue.x-k8s.io -o json |
  jq -S -c '[.items[] | {name: .metadata.name, spec: .spec}] | sort_by(.name)')"
queue_sha_before="$(printf %s "$queue_spec_before" | sha256sum | awk '{print $1}')"
flavor_sha_before="$(printf %s "$flavor_spec_before" | sha256sum | awk '{print $1}')"

create_completion_canary() {
  k create -f - >/dev/null <<YAML
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: $complete_name
  namespace: $FS2_JOBSET_NAMESPACE
  labels:
    app.kubernetes.io/managed-by: fs2-jobset-live-qualification
    fs2.nebius.ai/qualification: kubernetes-1-35
    kueue.x-k8s.io/queue-name: $FS2_JOBSET_QUEUE
    kueue.x-k8s.io/priority-class: standard
spec:
  suspend: true
  failurePolicy:
    maxRestarts: 0
  replicatedJobs:
    - name: workers
      replicas: 2
      template:
        spec:
          parallelism: 1
          completions: 1
          backoffLimit: 0
          activeDeadlineSeconds: 180
          template:
            metadata:
              labels:
                fs2.nebius.ai/qualification: kubernetes-1-35
            spec:
              restartPolicy: Never
              nodeSelector:
                workload.fs2.nebius/system: "true"
              containers:
                - name: worker
                  image: $canary_image
                  command: ["sh", "-c"]
                  args: ["printf 'jobset-live-1.35 completion\\n'; sleep 1"]
                  resources:
                    requests:
                      cpu: 10m
                      memory: 16Mi
                    limits:
                      cpu: 100m
                      memory: 32Mi
YAML
}

create_cleanup_canary() {
  k create -f - >/dev/null <<YAML
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: $cleanup_name
  namespace: $FS2_JOBSET_NAMESPACE
  labels:
    app.kubernetes.io/managed-by: fs2-jobset-live-qualification
    fs2.nebius.ai/qualification: kubernetes-1-35
    kueue.x-k8s.io/queue-name: $FS2_JOBSET_QUEUE
    kueue.x-k8s.io/priority-class: standard
spec:
  suspend: true
  failurePolicy:
    maxRestarts: 0
  replicatedJobs:
    - name: workers
      replicas: 2
      template:
        spec:
          parallelism: 1
          completions: 1
          backoffLimit: 0
          activeDeadlineSeconds: 600
          template:
            metadata:
              labels:
                fs2.nebius.ai/qualification: kubernetes-1-35
            spec:
              restartPolicy: Never
              terminationGracePeriodSeconds: 1
              nodeSelector:
                workload.fs2.nebius/system: "true"
              containers:
                - name: worker
                  image: $canary_image
                  command: ["sh", "-c"]
                  args: ["trap 'exit 0' TERM INT; sleep 600"]
                  resources:
                    requests:
                      cpu: 10m
                      memory: 16Mi
                    limits:
                      cpu: 100m
                      memory: 32Mi
YAML
}

wait_for_workload() {
  owner_uid="$1"
  found=""
  for _ in $(seq 1 90); do
    found="$(k -n "$FS2_JOBSET_NAMESPACE" get workloads.kueue.x-k8s.io -o json |
      jq -r --arg uid "$owner_uid" '
        .items[] |
        select(any(.metadata.ownerReferences[]?; .kind == "JobSet" and .uid == $uid)) |
        .metadata.name
      ' | head -1)"
    [[ -n "$found" ]] && break
    sleep 2
  done
  [[ -n "$found" ]] || return 1
  printf %s "$found"
}

wait_for_admission() {
  workload_name="$1"
  admitted=""
  for _ in $(seq 1 90); do
    admitted="$(k -n "$FS2_JOBSET_NAMESPACE" get workload "$workload_name" \
      -o jsonpath='{.status.conditions[?(@.type=="Admitted")].status}' 2>/dev/null || true)"
    [[ "$admitted" == "True" ]] && break
    sleep 2
  done
  [[ "$admitted" == "True" ]]
}

wait_for_two_running_pods() {
  jobset_name="$1"
  for _ in $(seq 1 90); do
    pod_state="$(k -n "$FS2_JOBSET_NAMESPACE" get pods \
      -l "jobset.sigs.k8s.io/jobset-name=$jobset_name" -o json |
      jq -r '[.items[].status.phase] | if length == 2 and all(. == "Running") then "ready" else "waiting" end')"
    [[ "$pod_state" == "ready" ]] && return 0
    sleep 2
  done
  return 1
}

wait_for_absence() {
  jobset_name="$1"
  workload_name="$2"
  for _ in $(seq 1 90); do
    jobset_count="$(k -n "$FS2_JOBSET_NAMESPACE" get jobset "$jobset_name" \
      --ignore-not-found -o name | wc -l)"
    jobs_count="$(k -n "$FS2_JOBSET_NAMESPACE" get jobs \
      -l "jobset.sigs.k8s.io/jobset-name=$jobset_name" -o name | wc -l)"
    pods_count="$(k -n "$FS2_JOBSET_NAMESPACE" get pods \
      -l "jobset.sigs.k8s.io/jobset-name=$jobset_name" -o name | wc -l)"
    workload_count="$(k -n "$FS2_JOBSET_NAMESPACE" get workload "$workload_name" \
      --ignore-not-found -o name | wc -l)"
    if [[ "$jobset_count" -eq 0 && "$jobs_count" -eq 0 && "$pods_count" -eq 0 && "$workload_count" -eq 0 ]]; then
      return 0
    fi
    sleep 2
  done
  return 1
}

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
create_completion_canary
complete_uid="$(k -n "$FS2_JOBSET_NAMESPACE" get jobset "$complete_name" -o jsonpath='{.metadata.uid}')"
complete_workload="$(wait_for_workload "$complete_uid")"
wait_for_admission "$complete_workload"
k -n "$FS2_JOBSET_NAMESPACE" wait --for=jsonpath='{.spec.suspend}'=false \
  --timeout=180s "jobset/$complete_name" >/dev/null
k -n "$FS2_JOBSET_NAMESPACE" wait --for=condition=Completed \
  --timeout=300s "jobset/$complete_name" >/dev/null
k -n "$FS2_JOBSET_NAMESPACE" get "jobset/$complete_name" -o json >"$work_dir/complete-jobset.json"
k -n "$FS2_JOBSET_NAMESPACE" get "workload/$complete_workload" -o json >"$work_dir/complete-workload.json"
k -n "$FS2_JOBSET_NAMESPACE" get jobs \
  -l "jobset.sigs.k8s.io/jobset-name=$complete_name" -o json >"$work_dir/complete-jobs.json"
k -n "$FS2_JOBSET_NAMESPACE" get pods \
  -l "jobset.sigs.k8s.io/jobset-name=$complete_name" -o json >"$work_dir/complete-pods.json"
jq -e '
  (.items | length) == 2 and
  all(.items[]; .status.succeeded == 1)
' "$work_dir/complete-jobs.json" >/dev/null
jq -e '
  (.items | length) == 2 and
  all(.items[];
    .status.phase == "Succeeded" and
    .spec.nodeSelector["workload.fs2.nebius/system"] == "true" and
    all(.spec.containers[]; (.resources.requests["nvidia.com/gpu"] // null) == null)
  )
' "$work_dir/complete-pods.json" >/dev/null
jq -e --arg queue "$cluster_queue" '
  .metadata.ownerReferences[0].kind == "JobSet" and
  .status.admission.clusterQueue == $queue and
  any(.status.conditions[]; .type == "QuotaReserved" and .status == "True") and
  any(.status.conditions[]; .type == "Admitted" and .status == "True") and
  .spec.podSets[0].count == 2
' "$work_dir/complete-workload.json" >/dev/null
complete_deleted_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
k -n "$FS2_JOBSET_NAMESPACE" delete "jobset/$complete_name" \
  --cascade=foreground --wait=true --timeout=120s >/dev/null
wait_for_absence "$complete_name" "$complete_workload"

create_cleanup_canary
cleanup_uid="$(k -n "$FS2_JOBSET_NAMESPACE" get jobset "$cleanup_name" -o jsonpath='{.metadata.uid}')"
cleanup_workload="$(wait_for_workload "$cleanup_uid")"
wait_for_admission "$cleanup_workload"
k -n "$FS2_JOBSET_NAMESPACE" wait --for=jsonpath='{.spec.suspend}'=false \
  --timeout=180s "jobset/$cleanup_name" >/dev/null
wait_for_two_running_pods "$cleanup_name"
k -n "$FS2_JOBSET_NAMESPACE" get "jobset/$cleanup_name" -o json >"$work_dir/cleanup-jobset.json"
k -n "$FS2_JOBSET_NAMESPACE" get "workload/$cleanup_workload" -o json >"$work_dir/cleanup-workload.json"
k -n "$FS2_JOBSET_NAMESPACE" get jobs \
  -l "jobset.sigs.k8s.io/jobset-name=$cleanup_name" -o json >"$work_dir/cleanup-jobs.json"
k -n "$FS2_JOBSET_NAMESPACE" get pods \
  -l "jobset.sigs.k8s.io/jobset-name=$cleanup_name" -o json >"$work_dir/cleanup-pods.json"
jq -e '(.items | length) == 2 and all(.items[]; .status.phase == "Running")' \
  "$work_dir/cleanup-pods.json" >/dev/null
cleanup_deleted_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
k -n "$FS2_JOBSET_NAMESPACE" delete "jobset/$cleanup_name" \
  --cascade=foreground --wait=true --timeout=120s >/dev/null
wait_for_absence "$cleanup_name" "$cleanup_workload"

queue_spec_after="$(k get clusterqueues.kueue.x-k8s.io -o json |
  jq -S -c '[.items[] | {name: .metadata.name, spec: .spec}] | sort_by(.name)')"
flavor_spec_after="$(k get resourceflavors.kueue.x-k8s.io -o json |
  jq -S -c '[.items[] | {name: .metadata.name, spec: .spec}] | sort_by(.name)')"
queue_sha_after="$(printf %s "$queue_spec_after" | sha256sum | awk '{print $1}')"
flavor_sha_after="$(printf %s "$flavor_spec_after" | sha256sum | awk '{print $1}')"
[[ "$queue_sha_before" == "$queue_sha_after" ]]
[[ "$flavor_sha_before" == "$flavor_sha_after" ]]

finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jq -n \
  --arg generated_at "$finished_at" \
  --arg started_at "$started_at" \
  --arg cluster_id "$FS2_JOBSET_CLUSTER_ID" \
  --arg context "$FS2_JOBSET_CONTEXT" \
  --arg server "$actual_server" \
  --arg namespace "$FS2_JOBSET_NAMESPACE" \
  --arg queue "$FS2_JOBSET_QUEUE" \
  --arg cluster_queue "$cluster_queue" \
  --arg chart_version "$jobset_chart_version" \
  --arg chart_digest "$jobset_chart_digest" \
  --arg archive_sha "$jobset_chart_archive_sha256" \
  --arg jobset_image "$jobset_image" \
  --arg kueue_version "$kueue_version" \
  --arg kueue_digest "$kueue_image_digest" \
  --arg canary_image "$canary_image" \
  --arg complete_deleted_at "$complete_deleted_at" \
  --arg cleanup_deleted_at "$cleanup_deleted_at" \
  --arg queue_before "$queue_sha_before" \
  --arg queue_after "$queue_sha_after" \
  --arg flavor_before "$flavor_sha_before" \
  --arg flavor_after "$flavor_sha_after" \
  --slurpfile crd "$work_dir/crd.json" \
  --slurpfile jobset_controller "$work_dir/jobset-controller.json" \
  --slurpfile kueue_controller "$work_dir/kueue-controller.json" \
  --slurpfile complete_jobset "$work_dir/complete-jobset.json" \
  --slurpfile complete_workload "$work_dir/complete-workload.json" \
  --slurpfile complete_jobs "$work_dir/complete-jobs.json" \
  --slurpfile complete_pods "$work_dir/complete-pods.json" \
  --slurpfile cleanup_jobset "$work_dir/cleanup-jobset.json" \
  --slurpfile cleanup_workload "$work_dir/cleanup-workload.json" \
  --slurpfile cleanup_jobs "$work_dir/cleanup-jobs.json" \
  --slurpfile cleanup_pods "$work_dir/cleanup-pods.json" '
  {
    schema: "fs2-serve.nebius.ai/jobset-kubernetes-1.35-live-qualification/v1",
    outcome: "PASS",
    started_at: $started_at,
    generated_at: $generated_at,
    target: {
      project_id: "project-e00rene",
      region: "eu-north1",
      cluster_id: $cluster_id,
      context: $context,
      kubernetes_server: $server,
      namespace: $namespace,
      local_queue: $queue,
      cluster_queue: $cluster_queue,
      compute: "CPU-only system node; zero GPU requests",
      b300_touched: false
    },
    releases: {
      jobset: {
        version: $chart_version,
        chart_digest: $chart_digest,
        chart_archive_sha256: $archive_sha,
        image: $jobset_image,
        deployment_uid: $jobset_controller[0].metadata.uid,
        deployment_generation: $jobset_controller[0].metadata.generation
      },
      kueue: {
        version: $kueue_version,
        image_digest: $kueue_digest,
        deployment_uid: $kueue_controller[0].metadata.uid,
        deployment_generation: $kueue_controller[0].metadata.generation
      },
      crd: {
        uid: $crd[0].metadata.uid,
        resource_version: $crd[0].metadata.resourceVersion,
        api: "jobset.x-k8s.io/v1alpha2",
        established: true
      },
      canary_image: $canary_image
    },
    canaries: {
      completion: {
        jobset: {
          name: $complete_jobset[0].metadata.name,
          uid: $complete_jobset[0].metadata.uid,
          created_at: $complete_jobset[0].metadata.creationTimestamp,
          completed_at: (
            $complete_jobset[0].status.conditions[] |
            select(.type == "Completed" and .status == "True") |
            .lastTransitionTime
          )
        },
        workload: {
          name: $complete_workload[0].metadata.name,
          uid: $complete_workload[0].metadata.uid,
          pod_set_count: $complete_workload[0].spec.podSets[0].count,
          cluster_queue: $complete_workload[0].status.admission.clusterQueue,
          quota_reserved_at: (
            $complete_workload[0].status.conditions[] |
            select(.type == "QuotaReserved" and .status == "True") |
            .lastTransitionTime
          ),
          admitted_at: (
            $complete_workload[0].status.conditions[] |
            select(.type == "Admitted" and .status == "True") |
            .lastTransitionTime
          )
        },
        child_jobs: [
          $complete_jobs[0].items[] |
          {name: .metadata.name, uid: .metadata.uid, succeeded: .status.succeeded}
        ],
        pods: [
          $complete_pods[0].items[] |
          {
            name: .metadata.name,
            uid: .metadata.uid,
            phase: .status.phase,
            node: .spec.nodeName,
            gpu_request: (.spec.containers[0].resources.requests["nvidia.com/gpu"] // null)
          }
        ],
        deleted_at: $complete_deleted_at,
        residual_jobsets: 0,
        residual_jobs: 0,
        residual_pods: 0,
        residual_workloads: 0
      },
      cleanup: {
        jobset: {
          name: $cleanup_jobset[0].metadata.name,
          uid: $cleanup_jobset[0].metadata.uid,
          created_at: $cleanup_jobset[0].metadata.creationTimestamp
        },
        workload: {
          name: $cleanup_workload[0].metadata.name,
          uid: $cleanup_workload[0].metadata.uid,
          pod_set_count: $cleanup_workload[0].spec.podSets[0].count,
          cluster_queue: $cleanup_workload[0].status.admission.clusterQueue,
          admitted_at: (
            $cleanup_workload[0].status.conditions[] |
            select(.type == "Admitted" and .status == "True") |
            .lastTransitionTime
          )
        },
        child_jobs_before_delete: [
          $cleanup_jobs[0].items[] |
          {name: .metadata.name, uid: .metadata.uid, active: .status.active}
        ],
        pods_before_delete: [
          $cleanup_pods[0].items[] |
          {
            name: .metadata.name,
            uid: .metadata.uid,
            phase: .status.phase,
            node: .spec.nodeName,
            gpu_request: (.spec.containers[0].resources.requests["nvidia.com/gpu"] // null)
          }
        ],
        deleted_at: $cleanup_deleted_at,
        residual_jobsets: 0,
        residual_jobs: 0,
        residual_pods: 0,
        residual_workloads: 0
      }
    },
    preservation: {
      cluster_queue_specs_sha256_before: $queue_before,
      cluster_queue_specs_sha256_after: $queue_after,
      resource_flavor_specs_sha256_before: $flavor_before,
      resource_flavor_specs_sha256_after: $flavor_after,
      quota_or_flavor_mutation: false,
      nodegroup_mutation: false,
      gpu_requested: false
    }
  }
'
