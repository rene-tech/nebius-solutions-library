#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
  cat >&2 <<'EOF'
usage: network-policy-transition.sh ACTION OPTIONS [-- HELM_VALUE_ARGS...]

Actions:
  stage       Render, server-dry-run, apply, and verify both temporary allows.
  complete    Verify normal allows equal their guards, then remove the guards.
  rollback    Relax the rendered gateway deny, then run a Helm rollback while
              retaining both guards.

Required options:
  --release NAME
  --release-namespace NAMESPACE
  --chart PATH
  --kubeconfig PATH

Optional options:
  --context NAME
  --values PATH          Repeat for ordinary Helm values files.
  --values-env NAME      Repeat for YAML supplied through an environment value.
  --revision NUMBER      Required by rollback.
  --timeout DURATION     Helm rollback timeout (default: 10m).
EOF
  exit 64
}

[[ $# -ge 1 ]] || usage
action="$1"
shift

release=""
release_namespace=""
chart=""
kubeconfig=""
kube_context=""
revision=""
rollback_timeout="10m"
declare -a values_files=()
declare -a values_envs=()
declare -a passthrough=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) release="${2:-}"; shift 2 ;;
    --release-namespace) release_namespace="${2:-}"; shift 2 ;;
    --chart) chart="${2:-}"; shift 2 ;;
    --kubeconfig) kubeconfig="${2:-}"; shift 2 ;;
    --context) kube_context="${2:-}"; shift 2 ;;
    --values) values_files+=("${2:-}"); shift 2 ;;
    --values-env) values_envs+=("${2:-}"); shift 2 ;;
    --revision) revision="${2:-}"; shift 2 ;;
    --timeout) rollback_timeout="${2:-}"; shift 2 ;;
    --) shift; passthrough=("$@"); break ;;
    *) usage ;;
  esac
done

[[ "${action}" =~ ^(stage|complete|rollback)$ ]] || usage
[[ -n "${release}" && -n "${release_namespace}" && -n "${chart}" && -n "${kubeconfig}" ]] || usage
[[ -d "${chart}" && -f "${kubeconfig}" ]] || {
  echo "chart and kubeconfig must exist" >&2
  exit 66
}
if [[ "${action}" == rollback && ! "${revision}" =~ ^[1-9][0-9]*$ ]]; then
  echo "rollback requires a positive --revision" >&2
  exit 64
fi

transition_dir="$(mktemp -d -t fs2-network-policy-transition.XXXXXX)"
cleanup() {
  rm -rf -- "${transition_dir}"
}
trap cleanup EXIT

declare -a kubectl_command=(kubectl --kubeconfig "${kubeconfig}")
declare -a helm_cluster_args=(--kubeconfig "${kubeconfig}")
if [[ -n "${kube_context}" ]]; then
  kubectl_command+=(--context "${kube_context}")
  helm_cluster_args+=(--kube-context "${kube_context}")
fi

declare -a helm_value_args=()
for values_file in "${values_files[@]}"; do
  [[ -f "${values_file}" ]] || {
    echo "values file does not exist: ${values_file}" >&2
    exit 66
  }
  helm_value_args+=(--values "${values_file}")
done
for index in "${!values_envs[@]}"; do
  environment_name="${values_envs[$index]}"
  [[ "${environment_name}" =~ ^[A-Z][A-Z0-9_]*$ ]] || {
    echo "invalid values environment name" >&2
    exit 64
  }
  [[ -v "${environment_name}" ]] || {
    echo "values environment is unset: ${environment_name}" >&2
    exit 64
  }
  values_path="${transition_dir}/values-${index}.yaml"
  printf '%s\n' "${!environment_name}" >"${values_path}"
  helm_value_args+=(--values "${values_path}")
done
helm_value_args+=("${passthrough[@]}")

rendered="${transition_dir}/rendered.yaml"
guards="${transition_dir}/guards.yaml"
helm template "${release}" "${chart}" \
  --namespace "${release_namespace}" \
  --show-only templates/networkpolicy.yaml \
  "${helm_value_args[@]}" \
  --set networkPolicy.transition.renderGuards=true >"${rendered}"

# Extract only uniquely named guards. The normal allows and deny are never
# applied by this phase; Helm remains their sole release owner.
awk '
  function emit() {
    if (document ~ /fs2\.nebius\.ai\/network-policy-transition: guard/) {
      printf "%s", document
    }
  }
  /^---[[:space:]]*$/ { emit(); document = $0 ORS; next }
  { document = document $0 ORS }
  END { emit() }
' "${rendered}" >"${guards}"

guard_count="$(grep -c 'fs2.nebius.ai/network-policy-transition: guard' "${guards}" || true)"
if [[ "${guard_count}" == 0 ]]; then
  if [[ "${action}" == rollback ]]; then
    # Internal-only releases render no public Gateway and therefore have no
    # gateway deny to relax. Keep the same governed rollback entry point.
    helm rollback "${release}" "${revision}" \
      --namespace "${release_namespace}" \
      "${helm_cluster_args[@]}" \
      --wait --wait-for-jobs --timeout "${rollback_timeout}"
    echo "network-policy-transition=rolled-back revision=${revision} public-gateway=disabled"
    exit 0
  fi
  echo "network-policy-transition=disabled release=${release}"
  exit 0
fi
[[ "${guard_count}" == 2 ]] || {
  echo "expected exactly two rendered transition guards, got ${guard_count}" >&2
  exit 65
}

guard_selector="app.kubernetes.io/instance=${release},fs2.nebius.ai/network-policy-transition=guard"

load_guards() {
  "${kubectl_command[@]}" get networkpolicy --all-namespaces \
    --selector "${guard_selector}" -o json
}

verify_guards() {
  local guard_json="$1"
  jq -e '
    (.items | length) == 2 and
    ([.items[].metadata.labels["fs2.nebius.ai/network-policy-role"]] | sort) ==
      ["envoy-controller", "public-envoy"] and
    all(.items[];
      (.metadata.namespace | type == "string" and length > 0) and
      (.metadata.annotations["fs2.nebius.ai/normal-policy-name"] | type == "string" and length > 0) and
      (.spec.podSelector | type == "object") and
      (.spec.policyTypes | length > 0)
    )
  ' <<<"${guard_json}" >/dev/null
}

if [[ "${action}" == stage ]]; then
  "${kubectl_command[@]}" apply --server-side --dry-run=server \
    --field-manager=fs2-network-policy-transition -f "${guards}" >/dev/null
  "${kubectl_command[@]}" apply --server-side \
    --field-manager=fs2-network-policy-transition -f "${guards}" >/dev/null
  current_guards="$(load_guards)"
  verify_guards "${current_guards}"
  echo "network-policy-transition=staged guards=2"
  exit 0
fi

current_guards="$(load_guards)"
verify_guards "${current_guards}"

verify_normal_allow() {
  local role="$1"
  local guard_json namespace normal_name normal_json
  guard_json="$(jq -ce --arg role "${role}" '.items[] | select(.metadata.labels["fs2.nebius.ai/network-policy-role"] == $role)' <<<"${current_guards}")"
  namespace="$(jq -er '.metadata.namespace' <<<"${guard_json}")"
  normal_name="$(jq -er '.metadata.annotations["fs2.nebius.ai/normal-policy-name"]' <<<"${guard_json}")"
  normal_json="$("${kubectl_command[@]}" get networkpolicy "${normal_name}" --namespace "${namespace}" -o json)"
  jq -e --argjson normal "${normal_json}" '.spec == $normal.spec' <<<"${guard_json}" >/dev/null
}

if [[ "${action}" == complete ]]; then
  verify_normal_allow public-envoy
  verify_normal_allow envoy-controller
  "${kubectl_command[@]}" delete -f "${guards}" --wait=true >/dev/null
  remaining="$(load_guards)"
  jq -e '.items | length == 0' <<<"${remaining}" >/dev/null
  echo "network-policy-transition=complete guards=0"
  exit 0
fi

proxy_guard="$(jq -ce '.items[] | select(.metadata.labels["fs2.nebius.ai/network-policy-role"] == "public-envoy")' <<<"${current_guards}")"
gateway_namespace="$(jq -er '.metadata.namespace' <<<"${proxy_guard}")"
deny_name="$(jq -er '.metadata.annotations["fs2.nebius.ai/deny-policy-name"]' <<<"${proxy_guard}")"
if "${kubectl_command[@]}" get networkpolicy "${deny_name}" --namespace "${gateway_namespace}" >/dev/null 2>&1; then
  "${kubectl_command[@]}" patch networkpolicy "${deny_name}" \
    --namespace "${gateway_namespace}" --type=merge \
    --patch '{"spec":{"podSelector":{"matchLabels":{"fs2.nebius.ai/rollback-relaxed":"true"}}}}' >/dev/null
  "${kubectl_command[@]}" get networkpolicy "${deny_name}" \
    --namespace "${gateway_namespace}" -o json \
    | jq -e '.spec.podSelector.matchLabels == {"fs2.nebius.ai/rollback-relaxed":"true"}' >/dev/null
  relaxed_matches="$("${kubectl_command[@]}" get pods --namespace "${gateway_namespace}" \
    --selector 'fs2.nebius.ai/rollback-relaxed=true' -o json | jq -er '.items | length')"
  [[ "${relaxed_matches}" == 0 ]] || {
    echo "relaxed deny selector unexpectedly matches Pods" >&2
    exit 65
  }
fi

# Recheck after relaxing the deny and immediately before Helm. These guards use
# distinct names, so Helm rollback and cleanup cannot remove them.
verify_guards "$(load_guards)"
helm rollback "${release}" "${revision}" \
  --namespace "${release_namespace}" \
  "${helm_cluster_args[@]}" \
  --wait --wait-for-jobs --timeout "${rollback_timeout}"
verify_guards "$(load_guards)"
echo "network-policy-transition=rolled-back revision=${revision} guards=retained gateway-namespace=${gateway_namespace}"
