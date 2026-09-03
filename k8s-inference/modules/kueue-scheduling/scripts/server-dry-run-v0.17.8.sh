#!/usr/bin/env bash
set -euo pipefail

kueue_version="v0.17.8"
kueue_chart_ref="oci://registry.k8s.io/kueue/charts/kueue"
kueue_chart_digest="sha256:e5f000fcf0604e5dea0025e0ffdd20e6712de432bcca0ec254d71d97f012a354"
kueue_chart_archive_sha256="409de6260d2b7834fece5044502822bcb4e74ed8a03b8ea22bb78bcdfa1627db"
kueue_image="registry.k8s.io/kueue/kueue:v0.17.8@sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6"
kind_node_image="kindest/node:v1.35.1@sha256:05d7bcdefbda08b4e038f644c4df690cdac3fba8b06f8289f30e10026720a1ab"
cluster_name="fs2-kueue-crd-$RANDOM"
work_dir="$(mktemp -d /tmp/fs2-kueue-crd.XXXXXX)"
kubeconfig="${work_dir}/kubeconfig"
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
production_values="${repository_root}/stages/foundation/values/kueue.yaml"

cleanup() {
  kind delete cluster --name "${cluster_name}" >/dev/null 2>&1 || true
  find "${work_dir}" -type f -delete
  find "${work_dir}" -depth -type d -empty -delete
}
trap cleanup EXIT

actual_chart_digest="$(crane digest "${kueue_chart_ref#oci://}@${kueue_chart_digest}")"
[[ "${actual_chart_digest}" == "${kueue_chart_digest}" ]]
actual_image_digest="$(crane digest "${kueue_image%%:*}@${kueue_image##*@}")"
[[ "${actual_image_digest}" == "${kueue_image##*@}" ]]
helm pull "${kueue_chart_ref}@${kueue_chart_digest}" --destination "${work_dir}"
kueue_chart="$(find "${work_dir}" -maxdepth 1 -type f -name '*.tgz' -print -quit)"
[[ "$(sha256sum "${kueue_chart}" | awk '{print $1}')" == "${kueue_chart_archive_sha256}" ]]

kind create cluster \
  --name "${cluster_name}" \
  --image "${kind_node_image}" \
  --kubeconfig "${kubeconfig}" \
  --wait 120s >/dev/null

kubectl --kubeconfig "${kubeconfig}" label nodes --all \
  workload.fs2.nebius/system=true \
  --overwrite >/dev/null

helm install kueue "${kueue_chart}" \
  --namespace kueue-system \
  --create-namespace \
  --kubeconfig "${kubeconfig}" \
  --values "${production_values}" \
  --wait \
  --timeout 180s >/dev/null
kubectl --kubeconfig "${kubeconfig}" wait \
  --for=condition=Established \
  crd/clusterqueues.kueue.x-k8s.io crd/localqueues.kueue.x-k8s.io \
  --timeout=120s >/dev/null
kubectl --kubeconfig "${kubeconfig}" -n kueue-system rollout status deployment/kueue-controller-manager \
  --timeout=180s >/dev/null
deployed_image="$(kubectl --kubeconfig "${kubeconfig}" \
  -n kueue-system get deployment/kueue-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].image}')"
[[ "${deployed_image}" == "${kueue_image}" ]]
if kubectl --kubeconfig "${kubeconfig}" get crd jobsets.jobset.x-k8s.io >/dev/null 2>&1; then
  echo "the Kueue-only server test unexpectedly installed the JobSet CRD" >&2
  exit 1
fi
manager_config="$(kubectl --kubeconfig "${kubeconfig}" -n kueue-system \
  get configmap/kueue-manager-config \
  -o 'jsonpath={.data.controller_manager_config\.yaml}')"
for required in \
  'apiVersion: config.kueue.x-k8s.io/v1beta2' \
  'admissionFairSharing:' \
  'waitForPodsReady:' \
  'jobset.x-k8s.io/jobset' \
  'excludeResourcePrefixes:' \
  'timestamp: Creation'; do
  grep -Fq "${required}" <<<"${manager_config}"
done
kubectl --kubeconfig "${kubeconfig}" create namespace fs2-models >/dev/null

# This is the exact default flavor-fungibility shape rendered by the module:
# MayStopSearch/TryNextFlavor without preference. The Kueue 0.17.8 API must
# accept it, while its CRD CEL must reject the previously rendered preference.
kubectl --kubeconfig "${kubeconfig}" apply --server-side -f - >/dev/null <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: example-regular
spec:
  nodeLabels:
    accelerator.fs2.nebius/pool-id: example-regular
  tolerations:
    - key: dedicated
      operator: Equal
      value: fs2-inference
      effect: NoSchedule
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: inference-shared
spec:
  fairSharing:
    weight: "1"
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "1"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: inference-accelerators
spec:
  cohortName: inference-shared
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: fs2-models
  queueingStrategy: BestEffortFIFO
  admissionScope:
    admissionMode: UsageBasedAdmissionFairSharing
  fairSharing:
    weight: "1"
  flavorFungibility:
    whenCanBorrow: MayStopSearch
    whenCanPreempt: TryNextFlavor
  preemption:
    reclaimWithinCohort: LowerPriority
    withinClusterQueue: LowerPriority
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "1"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: alternate-accelerators
spec:
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: fs2-models
  queueingStrategy: BestEffortFIFO
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "0"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: inference-models
  namespace: fs2-models
spec:
  clusterQueue: inference-accelerators
  fairSharing:
    weight: "1"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata:
  name: presentation
value: 1000
description: FS2 presentation workload priority
YAML

# Kueue 0.17.8 templates (rather than chart crds/) own the CRDs. Exercise the
# Helm upgrade path and prove it keeps an existing queue object while retaining
# a served+storage v1beta2 schema; a JobSet-style second CRD owner is forbidden.
kueue_release_manifest="$(helm get manifest kueue \
  --namespace kueue-system \
  --kubeconfig "${kubeconfig}")"
grep -Fq '# Source: kueue/templates/crd/' <<<"${kueue_release_manifest}"
queue_uid_before="$(kubectl --kubeconfig "${kubeconfig}" -n fs2-models \
  get localqueue/inference-models -o jsonpath='{.metadata.uid}')"
helm upgrade kueue "${kueue_chart}" \
  --namespace kueue-system \
  --kubeconfig "${kubeconfig}" \
  --values "${production_values}" \
  --wait \
  --timeout 180s >/dev/null
queue_uid_after="$(kubectl --kubeconfig "${kubeconfig}" -n fs2-models \
  get localqueue/inference-models -o jsonpath='{.metadata.uid}')"
[[ "${queue_uid_after}" == "${queue_uid_before}" ]]
kubectl --kubeconfig "${kubeconfig}" get crd clusterqueues.kueue.x-k8s.io -o json |
  python3 -c 'import json,sys; value=json.load(sys.stdin); versions=value["spec"]["versions"]; assert any(item["name"] == "v1beta2" and item["served"] and item["storage"] for item in versions); assert "v1beta2" in value["status"]["storedVersions"]'

# admissionChecksStrategy is schema-only coverage. This module deliberately does
# not install an AdmissionCheck or its controller; the deployable fixture above
# therefore keeps the strategy absent so its ClusterQueue can become active.
kubectl --kubeconfig "${kubeconfig}" apply --server-side --dry-run=server -f - >/dev/null <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: admission-check-schema-only
spec:
  namespaceSelector: {}
  queueingStrategy: BestEffortFIFO
  admissionChecksStrategy:
    admissionChecks:
      - name: externally-managed-check
        onFlavors: [example-regular]
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "0"
YAML

# The exact allowed tuple the module renders for a queue that sets a
# preference: both search directions TryNextFlavor. Kueue 0.17.8 must accept it.
kubectl --kubeconfig "${kubeconfig}" apply --server-side --dry-run=server -f - >/dev/null <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: preference-tuple-accepted
spec:
  namespaceSelector:
    matchExpressions:
      - key: kubernetes.io/metadata.name
        operator: In
        values: [fs2-models]
  queueingStrategy: BestEffortFIFO
  flavorFungibility:
    whenCanBorrow: TryNextFlavor
    whenCanPreempt: TryNextFlavor
    preference: PreemptionOverBorrowing
  preemption:
    reclaimWithinCohort: LowerPriority
    withinClusterQueue: LowerPriority
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "1"
YAML
kubectl --kubeconfig "${kubeconfig}" apply --server-side --dry-run=server -f - >/dev/null <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: borrowing-preference-accepted
spec:
  namespaceSelector:
    matchExpressions:
      - key: kubernetes.io/metadata.name
        operator: In
        values: [fs2-models]
  queueingStrategy: BestEffortFIFO
  flavorFungibility:
    whenCanBorrow: TryNextFlavor
    whenCanPreempt: TryNextFlavor
    preference: BorrowingOverPreemption
  preemption:
    reclaimWithinCohort: LowerPriority
    withinClusterQueue: LowerPriority
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "1"
YAML

if kubectl --kubeconfig "${kubeconfig}" apply --server-side --dry-run=server -f - >/dev/null 2>"${work_dir}/invalid-preference.log" <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: inference-accelerators
spec:
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: fs2-models
  queueingStrategy: BestEffortFIFO
  flavorFungibility:
    whenCanBorrow: MayStopSearch
    whenCanPreempt: TryNextFlavor
    preference: BorrowingOverPreemption
  preemption:
    reclaimWithinCohort: LowerPriority
    withinClusterQueue: LowerPriority
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "1"
YAML
then
  echo "Kueue accepted the forbidden flavorFungibility preference combination" >&2
  exit 1
fi
grep -Eq 'whenCanBorrow.*TryNextFlavor|whenCanPreempt.*TryNextFlavor|Invalid value' "${work_dir}/invalid-preference.log"

if kubectl --kubeconfig "${kubeconfig}" apply --server-side --dry-run=server -f - >/dev/null 2>"${work_dir}/invalid-fair-sharing-weight.log" <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: invalid-fair-sharing-weight
spec:
  fairSharing:
    weight: "0.000000001"
YAML
then
  echo "Kueue accepted a fair-sharing weight at the forbidden 1e-9 floor" >&2
  exit 1
fi
grep -Eqi 'weight must be greater than|1n|Invalid value' "${work_dir}/invalid-fair-sharing-weight.log"

# --force-conflicts matters here: a server-side apply from a second field
# manager is otherwise rejected for field ownership, and that rejection would
# pass as immutability proof while proving nothing. Forcing ownership leaves
# the CEL immutability rule as the only thing that can still refuse it.
if kubectl --kubeconfig "${kubeconfig}" apply --server-side --force-conflicts --dry-run=server -f - >/dev/null 2>"${work_dir}/immutable-localqueue.log" <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: inference-models
  namespace: fs2-models
spec:
  clusterQueue: alternate-accelerators
YAML
then
  echo "Kueue accepted an in-place LocalQueue ClusterQueue rebind" >&2
  exit 1
fi
cat "${work_dir}/immutable-localqueue.log" >&2
# The immutability rule by name. A looser pattern would accept any validation
# error, including one that has nothing to do with the binding.
grep -q 'field is immutable' "${work_dir}/immutable-localqueue.log"

printf 'Kueue CRD dry-run passed version=%s chart=%s image=%s server=v1.35.1 production-config=loaded helm-crd-upgrade=retained jobset-crd=absent default-preference=omitted fair-sharing-min=rejected localqueue-rebind=rejected\n' \
  "${kueue_version}" "${actual_chart_digest}" "${kueue_image}"
