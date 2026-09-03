#!/usr/bin/env bash
# Prove the production LocalQueue rebind semantics against a real Kueue API.
#
# LocalQueue.spec.clusterQueue is immutable, so a changed binding must plan a
# replacement rather than an in-place update the API server rejects. The two
# resource blocks under test are extracted verbatim from
# stages/workloads/queue.tf, so this exercises the production resource type,
# the production address, and the real kubernetes provider against a live
# Kueue CRD, not a stand-in.
#
# It creates and deletes its own Kind cluster and touches no cloud project,
# shared cluster, or live queue.
set -euo pipefail

kueue_chart_ref="oci://registry.k8s.io/kueue/charts/kueue"
kueue_chart_digest="sha256:e5f000fcf0604e5dea0025e0ffdd20e6712de432bcca0ec254d71d97f012a354"
kueue_chart_archive_sha256="409de6260d2b7834fece5044502822bcb4e74ed8a03b8ea22bb78bcdfa1627db"
kind_node_image="kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a"
cluster_name="fs2-lq-rebind-$RANDOM"
work_dir="$(mktemp -d /tmp/fs2-lq-rebind.XXXXXX)"
kubeconfig="${work_dir}/kubeconfig"
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
queue_source="${repository_root}/stages/workloads/queue.tf"
production_values="${repository_root}/stages/foundation/values/kueue.yaml"

cleanup() {
  kind delete cluster --name "${cluster_name}" >/dev/null 2>&1 || true
  find "${work_dir}" -type f -delete
  find "${work_dir}" -depth -type d -empty -delete
}
trap cleanup EXIT

helm pull "${kueue_chart_ref}@${kueue_chart_digest}" --destination "${work_dir}" >/dev/null
kueue_chart="$(find "${work_dir}" -maxdepth 1 -type f -name 'kueue*.tgz' -print -quit)"
[[ "$(sha256sum "${kueue_chart}" | awk '{print $1}')" == "${kueue_chart_archive_sha256}" ]]

kind create cluster \
  --name "${cluster_name}" \
  --image "${kind_node_image}" \
  --kubeconfig "${kubeconfig}" \
  --wait 180s >/dev/null
kubectl=(kubectl --kubeconfig "${kubeconfig}" --request-timeout=60s)
"${kubectl[@]}" label nodes --all workload.fs2.nebius/system=true --overwrite >/dev/null
helm install kueue "${kueue_chart}" \
  --namespace kueue-system --create-namespace \
  --kubeconfig "${kubeconfig}" --values "${production_values}" \
  --wait --timeout 300s >/dev/null
"${kubectl[@]}" -n kueue-system rollout status deployment/kueue-controller-manager --timeout=300s >/dev/null
"${kubectl[@]}" create namespace fs2-models >/dev/null

# The admission webhook has to be answering before any Kueue object is applied.
for _ in $(seq 1 60); do
  if "${kubectl[@]}" apply --server-side --dry-run=server -f - >/dev/null 2>&1 <<'PROBE'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: webhook-readiness-probe
PROBE
  then
    break
  fi
  sleep 2
done

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
  name: customer-batch
spec:
  namespaceSelector:
    matchExpressions:
      - key: kubernetes.io/metadata.name
        operator: In
        values: [fs2-models]
  queueingStrategy: BestEffortFIFO
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
  name: cancer-dedicated
spec:
  namespaceSelector:
    matchExpressions:
      - key: kubernetes.io/metadata.name
        operator: In
        values: [fs2-models]
  queueingStrategy: BestEffortFIFO
  resourceGroups:
    - coveredResources: [example.com/accelerator]
      flavors:
        - name: example-regular
          resources:
            - name: example.com/accelerator
              nominalQuota: "1"
YAML

# Extract the two resource blocks under test verbatim from production.
module_dir="${work_dir}/module"
mkdir -p "${module_dir}"
python3 - "${queue_source}" "${module_dir}/main.tf" "${kubeconfig}" <<'PY'
import re
import sys

source, target, kubeconfig = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(source, encoding="utf-8").read()


def block(header: str) -> str:
    start = text.index(header)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise SystemExit(f"unterminated block: {header}")


binding = block('resource "terraform_data" "additional_local_queue_binding" {')
manifest = block('resource "kubernetes_manifest" "additional_local_queue" {')

# The blocks reference the module contract and sibling resources that do not
# exist here. Feed them the same shapes from variables instead, without
# touching the resource type, address, or lifecycle wiring under test.
for original, replacement in (
    (
        """  for_each = {
    for queue_name, manifest in module.kueue_scheduling.contract.local_queues :
    queue_name => manifest
    if queue_name != local.queue_default.local_queue_name && !contains(
      module.kueue_scheduling.contract.external_local_queue_names, queue_name
    )
  }""",
        "  for_each = var.local_queues",
    ),
):
    binding = binding.replace(original, replacement)
    manifest = manifest.replace(original, replacement)
manifest = re.sub(r"\n  depends_on = \[(?:[^]]*)\]\n", "\n", manifest)

open(target, "w", encoding="utf-8").write(
    f'''terraform {{
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {{
    kubernetes = {{
      source  = "hashicorp/kubernetes"
      version = "= 3.2.1"
    }}
  }}
}}

provider "kubernetes" {{
  config_path = "{kubeconfig}"
}}

variable "local_queues" {{ type = any }}

{binding}

{manifest}
'''
)
print("extracted production resource blocks")
PY

queue_manifest() {
  python3 -c '
import json, sys
print(json.dumps({"local_queues": {"cancer-primary": {
  "apiVersion": "kueue.x-k8s.io/v1beta2",
  "kind": "LocalQueue",
  "metadata": {"name": "cancer-primary", "namespace": "fs2-models", "labels": {}, "annotations": {}},
  "spec": {"clusterQueue": sys.argv[1], "fairSharing": {"weight": "1"}},
}}}))' "$1"
}

terraform -chdir="${module_dir}" init -backend=false -input=false -no-color >/dev/null
queue_manifest customer-batch >"${module_dir}/first.tfvars.json"
terraform -chdir="${module_dir}" apply -auto-approve -input=false -no-color \
  -var-file=first.tfvars.json >/dev/null
uid_before="$("${kubectl[@]}" -n fs2-models get localqueue/cancer-primary -o jsonpath='{.metadata.uid}')"
[[ -n "${uid_before}" ]]

# An unchanged apply must be a no-op, so replacement is caused by the rebind
# and not by ordinary drift.
terraform -chdir="${module_dir}" plan -input=false -no-color -detailed-exitcode \
  -var-file=first.tfvars.json >/dev/null && idempotent=0 || idempotent=$?
[[ "${idempotent}" -eq 0 ]] || { echo "an unchanged apply was not a no-op" >&2; exit 1; }

queue_manifest cancer-dedicated >"${module_dir}/second.tfvars.json"
terraform -chdir="${module_dir}" plan -input=false -no-color -out=rebind.tfplan \
  -var-file=second.tfvars.json >/dev/null
terraform -chdir="${module_dir}" show -json rebind.tfplan >"${module_dir}/rebind.json"
python3 - "${module_dir}/rebind.json" <<'PY'
import json
import sys

changes = {
    item["address"]: item["change"]["actions"]
    for item in json.load(open(sys.argv[1], encoding="utf-8"))["resource_changes"]
}
manifest = 'kubernetes_manifest.additional_local_queue["cancer-primary"]'
binding = 'terraform_data.additional_local_queue_binding["cancer-primary"]'
for address in (manifest, binding):
    actions = changes.get(address)
    assert actions is not None, (address, sorted(changes))
    assert actions in (["delete", "create"], ["create", "delete"]), (address, actions)
print("replacement planned at %s" % manifest)
PY

echo "step: api-inplace-rebind" >&2
# The API itself refuses an in-place rebind, which is why replacement is the
# only correct plan.
#
# --force-conflicts matters: without it a server-side apply from a second
# field manager is rejected for field ownership, which looks like a rejection
# but proves nothing about immutability. Forcing ownership leaves the CEL
# immutability rule as the only thing that can still refuse the update.
if "${kubectl[@]}" apply --server-side --force-conflicts --dry-run=server -f - >/dev/null 2>"${work_dir}/rebind.log" <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: cancer-primary
  namespace: fs2-models
spec:
  clusterQueue: cancer-dedicated
YAML
then
  echo "Kueue accepted an in-place LocalQueue ClusterQueue rebind" >&2
  exit 1
fi
# Print what the API actually said, so a changed upstream message is visible
# rather than an unexplained exit.
cat "${work_dir}/rebind.log" >&2
# Require the immutability rule by name. A looser pattern would accept a
# field-ownership conflict or any validation error as proof.
grep -q 'field is immutable' "${work_dir}/rebind.log"

echo "step: uid-stability" >&2
uid_after="$("${kubectl[@]}" -n fs2-models get localqueue/cancer-primary -o jsonpath='{.metadata.uid}')"
[[ "${uid_after}" == "${uid_before}" ]]

# Measure, rather than assume, which ResourceFlavor fields this exact Kueue
# treats as immutable. Terraform should force a replacement only for a field
# the API really refuses to update, because a replacement briefly removes the
# flavor and stalls work admitted against it.
#
# What this measures: a flavor with no topologyName set. Kueue guards
# topologyName with a CEL rule that only applies once oldSelf.topologyName is
# already set, so these results say that non-topology flavors update in place.
# They do not say topologyName is universally mutable. A topology-aware flavor
# would need a replacement path this module does not yet have; the second
# probe pass below measures that state directly.
flavor_immutability=""
probe_flavor() {
  local field="$1" body="$2"
  if printf '%s' "${body}" | "${kubectl[@]}" apply --server-side --dry-run=server -f - >/dev/null 2>"${work_dir}/flavor-${field}.log"; then
    flavor_immutability="${flavor_immutability} ${field}=mutable"
  else
    flavor_immutability="${flavor_immutability} ${field}=immutable"
    cat "${work_dir}/flavor-${field}.log" >&2
  fi
}
probe_flavor nodeLabels 'apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: example-regular
spec:
  nodeLabels:
    workload.fs2.nebius/system: "false"
'
probe_flavor tolerations 'apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: example-regular
spec:
  nodeLabels:
    workload.fs2.nebius/system: "true"
  tolerations:
    - key: dedicated
      operator: Equal
      value: fs2-inference
      effect: NoSchedule
'
probe_flavor topologyName 'apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: example-regular
spec:
  nodeLabels:
    workload.fs2.nebius/system: "true"
  topologyName: example-topology
'

# Every owner that renders a LocalQueue must carry the same replacement
# identity, or its plan would attempt the in-place update this probe just
# proved the API refuses. Checked here rather than trusted, so a new owner
# cannot be added without one.
repository_root_owners=(
  "stages/workloads/queue.tf:terraform_data.additional_local_queue_binding"
  "stages/workloads/queue.tf:terraform_data.model_local_queue_binding"
  "stages/workloads/general_cpu.tf:terraform_data.general_cpu_local_queue_binding"
  "modules/academic-assets/main.tf:terraform_data.academic_local_queue_binding"
  "reference-data/terraform/main.tf:terraform_data.local_queue_binding"
)
for owner in "${repository_root_owners[@]}"; do
  source_file="${repository_root}/${owner%%:*}"
  binding="${owner##*:}"
  grep -q "replace_triggered_by = \[${binding}" "${source_file}" || {
    echo "LocalQueue owner ${source_file} has no replacement identity for ${binding}" >&2
    exit 1
  }
done
echo "every LocalQueue owner plans a replacement" >&2

printf 'LocalQueue rebind proved provider=kubernetes resource=kubernetes_manifest address=%s unchanged_plan=no-op rebind_plan=replace api_inplace=rejected uid_stable=%s\n' \
  'kubernetes_manifest.additional_local_queue["cancer-primary"]' "${uid_after}"
# Second state: a flavor that already has topologyName set. The CEL rule is
# self.topologyName == oldSelf.topologyName once oldSelf has one, so this is
# the state where a change really is refused.
"${kubectl[@]}" apply --server-side -f - >/dev/null <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: example-topology-aware
spec:
  nodeLabels:
    workload.fs2.nebius/system: "true"
  topologyName: example-topology
YAML
topology_change_rejected=no
if ! "${kubectl[@]}" apply --server-side --force-conflicts --dry-run=server -f - \
  >/dev/null 2>"${work_dir}/flavor-topology-set.log" <<'YAML'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: example-topology-aware
spec:
  nodeLabels:
    workload.fs2.nebius/system: "true"
  topologyName: example-other-topology
YAML
then
  cat "${work_dir}/flavor-topology-set.log" >&2
  grep -q 'immutable' "${work_dir}/flavor-topology-set.log" && topology_change_rejected=yes
fi

printf 'ResourceFlavor field mutability (no topologyName set):%s\n' "${flavor_immutability}"
printf 'ResourceFlavor topologyName change once set rejected=%s\n' "${topology_change_rejected}"
