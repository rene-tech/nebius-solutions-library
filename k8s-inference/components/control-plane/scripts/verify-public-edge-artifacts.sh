#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
control_root="$(cd "${script_dir}/.." && pwd)"
contract="${control_root}/contracts/public-edge-artifact-observations.json"
verify_dir="$(mktemp -d)"
trap 'rm -rf "${verify_dir}"' EXIT

for executable in curl git helm jq rg sha256sum tar; do
  command -v "${executable}" >/dev/null
done

verify_chart() {
  local component="$1"
  local reference="$2"
  local version="$3"
  local archive="$4"
  local expected_archive expected_manifest pull_output actual_archive actual_manifest
  expected_archive="$(jq -er ".${component}.chart_sha256" "${contract}")"
  expected_manifest="$(jq -er ".${component}.oci_manifest_sha256" "${contract}")"
  pull_output="$(helm pull "${reference}" --version "${version}" --destination "${verify_dir}" 2>&1)"
  actual_archive="$(sha256sum "${verify_dir}/${archive}" | cut -d' ' -f1)"
  actual_manifest="$(sed -n 's/^Digest: sha256://p' <<<"${pull_output}")"
  [[ "${actual_archive}" == "${expected_archive}" ]]
  [[ "${actual_manifest}" == "${expected_manifest}" ]]
  tar -xzf "${verify_dir}/${archive}" -C "${verify_dir}"
}

verify_source() {
  local component="$1"
  local repository="$2"
  local tag="$3"
  local destination="${verify_dir}/${component}"
  local expected_commit expected_tree
  expected_commit="$(jq -er ".${component}.source_commit" "${contract}")"
  expected_tree="$(jq -er ".${component}.source_tree" "${contract}")"
  git -c advice.detachedHead=false clone --quiet --depth=1 --branch "${tag}" "${repository}" "${destination}"
  [[ "$(git -C "${destination}" rev-parse 'HEAD^{commit}')" == "${expected_commit}" ]]
  [[ "$(git -C "${destination}" rev-parse 'HEAD^{tree}')" == "${expected_tree}" ]]
  while IFS=$'\t' read -r path expected_sha; do
    [[ "$(sha256sum "${destination}/${path}" | cut -d' ' -f1)" == "${expected_sha}" ]]
  done < <(jq -er ".${component}.opened_source_sha256 | to_entries[] | [.key,.value] | @tsv" "${contract}")
}

verify_chart envoy_gateway oci://docker.io/envoyproxy/gateway-helm v1.8.3 gateway-helm-v1.8.3.tgz
verify_chart cert_manager oci://quay.io/jetstack/charts/cert-manager v1.21.1 cert-manager-v1.21.1.tgz
verify_source envoy_gateway https://github.com/envoyproxy/gateway.git v1.8.3
verify_source cert_manager https://github.com/cert-manager/cert-manager.git v1.21.1

rg -q 'OwningGatewayNamespaceLabel = "gateway.envoyproxy.io/owning-gateway-namespace"' \
  "${verify_dir}/envoy_gateway/internal/gatewayapi/translator.go"
rg -q 'OwningGatewayNameLabel = "gateway.envoyproxy.io/owning-gateway-name"' \
  "${verify_dir}/envoy_gateway/internal/gatewayapi/translator.go"
rg -q 'GatewayNameLabel = "gateway.networking.k8s.io/gateway-name"' \
  "${verify_dir}/envoy_gateway/internal/gatewayapi/translator.go"
rg -q 'wellKnownPortShift = 10000' "${verify_dir}/envoy_gateway/internal/gatewayapi/translator.go"
rg -q 'return servicePort \+ wellKnownPortShift' "${verify_dir}/envoy_gateway/internal/gatewayapi/listener.go"
envoy_proxy_crd="${verify_dir}/gateway-helm/charts/crds/crds/generated/gateway.envoyproxy.io_envoyproxies.yaml"
rg -q 'ExternalTrafficPolicy determines the externalTrafficPolicy for the Envoy Service' "${envoy_proxy_crd}"
rg -q 'If the caller requests specific NodePorts' "${envoy_proxy_crd}"
rg -q 'Patch defines how to perform the patch operation' "${envoy_proxy_crd}"

rg -q 'SolverIdentificationLabelKey = "acme.cert-manager.io/http01-solver"' \
  "${verify_dir}/cert_manager/pkg/apis/acme/v1/types.go"
rg -q 'solverIdent := "true"' "${verify_dir}/cert_manager/pkg/issuer/acme/http/pod.go"
rg -q 'acmeSolverListenPort = 8089' "${verify_dir}/cert_manager/pkg/issuer/acme/http/http.go"
rg -q 'ParentRefs: ch.Spec.Solver.HTTP01.GatewayHTTPRoute.ParentRefs' \
  "${verify_dir}/cert_manager/pkg/issuer/acme/http/httproute.go"
rg -q 'PathMatchExact' "${verify_dir}/cert_manager/pkg/issuer/acme/http/httproute.go"

echo "public-edge-artifacts=PASS envoy-gateway=v1.8.3 cert-manager=v1.21.1"
