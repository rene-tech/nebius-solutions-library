#!/usr/bin/env bash
set -euo pipefail

: "${FS2_JOBSET_KUBECONFIG:?required}"
: "${FS2_JOBSET_CONTEXT:?required}"
: "${FS2_JOBSET_RUN_ROOT:?required}"
: "${FS2_JOBSET_CLUSTER_ID:?required}"
: "${FS2_JOBSET_NAMESPACE:?required}"
: "${FS2_JOBSET_CONTROLLER:?required}"
: "${FS2_JOBSET_KUBERNETES_MINOR:?required}"
: "${FS2_JOBSET_TIMEOUT_SECONDS:?required}"

case "${FS2_JOBSET_KUBECONFIG}" in
  "${FS2_JOBSET_RUN_ROOT}/kubeconfig") ;;
  *) echo "JobSet probe rejected a kubeconfig outside the run root" >&2; exit 2 ;;
esac
case "${FS2_JOBSET_NAMESPACE}" in
  jobset-system) ;;
  *) echo "JobSet probe rejected an unexpected namespace" >&2; exit 2 ;;
esac
case "${FS2_JOBSET_TIMEOUT_SECONDS}" in
  ''|*[!0-9]*) echo "JobSet probe timeout must be numeric" >&2; exit 2 ;;
esac

kubectl=(kubectl --kubeconfig "${FS2_JOBSET_KUBECONFIG}" --context "${FS2_JOBSET_CONTEXT}")
server_version="$("${kubectl[@]}" version -o json | jq -r '.serverVersion.gitVersion')"
server_minor="$(sed -E 's/^v?([0-9]+\.[0-9]+).*/v\1/' <<<"${server_version}")"
if [[ "${server_minor}" != "${FS2_JOBSET_KUBERNETES_MINOR}" ]]; then
  echo "JobSet compatibility gate expected Kubernetes minor ${FS2_JOBSET_KUBERNETES_MINOR}, got ${server_version}" >&2
  exit 1
fi

deadline=$((SECONDS + FS2_JOBSET_TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  established="$("${kubectl[@]}" get crd jobsets.jobset.x-k8s.io -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' 2>/dev/null || true)"
  available="$("${kubectl[@]}" -n "${FS2_JOBSET_NAMESPACE}" get deployment "${FS2_JOBSET_CONTROLLER}" -o jsonpath='{.status.availableReplicas}' 2>/dev/null || true)"
  discovered="$("${kubectl[@]}" api-resources --api-group=jobset.x-k8s.io -o name 2>/dev/null | tr '\n' ' ' || true)"
  if [[ "${established}" == "True" && "${available}" =~ ^[1-9][0-9]*$ && " ${discovered} " == *" jobsets.jobset.x-k8s.io "* ]]; then
    break
  fi
  sleep 2
done

if (( SECONDS >= deadline )); then
  echo "JobSet v1alpha2 API/controller did not become ready" >&2
  exit 1
fi

"${kubectl[@]}" create --dry-run=server -f - >/dev/null <<'YAML'
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: fs2-jobset-api-probe
  namespace: jobset-system
spec:
  replicatedJobs:
    - name: worker
      replicas: 2
      template:
        spec:
          parallelism: 1
          completions: 1
          template:
            spec:
              restartPolicy: Never
              containers:
                - name: worker
                  image: registry.k8s.io/pause:3.10
YAML

printf 'JobSet API ready cluster=%s server=%s api=jobset.x-k8s.io/v1alpha2 controller=%s\n' \
  "${FS2_JOBSET_CLUSTER_ID}" "${server_version}" "${FS2_JOBSET_CONTROLLER}"
