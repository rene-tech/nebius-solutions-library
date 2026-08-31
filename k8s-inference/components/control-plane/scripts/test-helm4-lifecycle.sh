#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

control_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$(cd "${control_root}/../../.." && pwd)"
chart="${repo_root}/k8s-inference/charts/control-plane/fs2-serve-control-plane"
test_dir="$(mktemp -d -t fs2-gateway-helm4.XXXXXX)"
kind_name="fs2-gateway-helm4-${RANDOM}-${RANDOM}"
registry_name="fs2-gateway-registry-${RANDOM}-${RANDOM}"
registry_image="registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
busybox_image="busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
kind_image="kindest/node:v1.35.0@sha256:4613778f3cfcd10e615029370f5786704559103cf27bef934597ba562b269661"

if [[ "$(helm version --template '{{.Version}}')" != v4.* ]]; then
  echo "gateway lifecycle test requires Helm 4" >&2
  exit 1
fi

cleanup() {
  kind delete cluster --name "${kind_name}" >/dev/null 2>&1 || true
  docker rm -f "${registry_name}" >/dev/null 2>&1 || true
  rm -rf -- "${test_dir}"
}
on_error() {
  local exit_code="$1" line_number="$2" failed_command="$3"
  echo "gateway Helm 4 lifecycle failed at line ${line_number}: ${failed_command}" >&2
  exit "${exit_code}"
}
trap cleanup EXIT
trap 'on_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

docker run -d --name "${registry_name}" -p 127.0.0.1::5000 "${registry_image}" >/dev/null
registry_port="$(docker port "${registry_name}" 5000/tcp | sed -E 's/.*:([0-9]+)$/\1/')"
if [[ ! "${registry_port}" =~ ^[0-9]+$ ]]; then
  echo "could not resolve test registry port" >&2
  exit 1
fi

cat >"${test_dir}/kind.yaml" <<YAML
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
YAML
timeout --signal=TERM --kill-after=30s 240s \
  kind create cluster --name "${kind_name}" --image "${kind_image}" \
    --config "${test_dir}/kind.yaml" --wait 90s >/dev/null
kind get kubeconfig --name "${kind_name}" >"${test_dir}/kubeconfig"
chmod 0600 "${test_dir}/kubeconfig"
export KUBECONFIG="${test_dir}/kubeconfig"

image_repository="localhost:${registry_port}/fs2-gateway-lifecycle"
lock_sha="$(sha256sum "${control_root}/uv.lock" | awk '{print $1}')"
dockerfile_sha="$(sha256sum "${control_root}/Dockerfile" | awk '{print $1}')"
context_sha="$(sha256sum "${control_root}/Dockerfile.dockerignore" | awk '{print $1}')"
DOCKER_BUILDKIT=1 docker build --quiet \
  --file "${control_root}/Dockerfile" \
  --tag "${image_repository}:candidate" \
  --build-arg FS2_SOURCE_COMMIT=unpublished-helm4-lifecycle-test \
  --build-arg FS2_SOURCE_TREE=unpublished-helm4-lifecycle-test \
  --build-arg "FS2_UV_LOCK_SHA256=${lock_sha}" \
  --build-arg "FS2_DOCKERFILE_SHA256=${dockerfile_sha}" \
  --build-arg "FS2_CONTEXT_POLICY_SHA256=${context_sha}" \
  "${repo_root}" >/dev/null
candidate_push="$(docker push "${image_repository}:candidate")"
candidate_digest="$(printf '%s\n' "${candidate_push}" | sed -nE 's/.*digest: (sha256:[a-f0-9]{64}).*/\1/p' | tail -n 1)"
if [[ ! "${candidate_digest}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "candidate registry digest is unavailable" >&2
  exit 1
fi

admin_image_repository="localhost:${registry_port}/fs2-admin-console-lifecycle"
admin_root="${repo_root}/k8s-inference/components/admin-console"
admin_lock_sha="$(sha256sum "${admin_root}/package-lock.json" | awk '{print $1}')"
admin_source_commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
admin_source_tree="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
admin_sbom_sha="cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
DOCKER_BUILDKIT=1 docker build --quiet \
  --file "${admin_root}/Dockerfile" \
  --tag "${admin_image_repository}:candidate" \
  --build-arg "FS2_SOURCE_COMMIT=${admin_source_commit}" \
  --build-arg "FS2_SOURCE_TREE=${admin_source_tree}" \
  --build-arg "FS2_PACKAGE_LOCK_SHA256=${admin_lock_sha}" \
  "${repo_root}" >/dev/null
admin_push="$(docker push "${admin_image_repository}:candidate")"
admin_digest="$(printf '%s\n' "${admin_push}" | sed -nE 's/.*digest: (sha256:[a-f0-9]{64}).*/\1/p' | tail -n 1)"
if [[ ! "${admin_digest}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "admin console registry digest is unavailable" >&2
  exit 1
fi

docker pull "${busybox_image}" >/dev/null
docker tag "${busybox_image}" "${image_repository}:failing-migration"
failure_push="$(docker push "${image_repository}:failing-migration")"
failure_digest="$(printf '%s\n' "${failure_push}" | sed -nE 's/.*digest: (sha256:[a-f0-9]{64}).*/\1/p' | tail -n 1)"
if [[ ! "${failure_digest}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "failure fixture registry digest is unavailable" >&2
  exit 1
fi

# Connecting this dual-stack registry to the kind network changes its default
# route on some Docker versions, so finish all host-side pushes first.
docker network connect kind "${registry_name}"
for node in $(kind get nodes --name "${kind_name}"); do
  registry_dir="/etc/containerd/certs.d/localhost:${registry_port}"
  docker exec "${node}" mkdir -p "${registry_dir}"
  docker exec -i "${node}" tee "${registry_dir}/hosts.toml" >/dev/null <<TOML
server = "http://${registry_name}:5000"

[host."http://${registry_name}:5000"]
  capabilities = ["pull", "resolve", "push"]
TOML
done

for namespace in fs2-system fs2-data fs2-observability fs2-models; do
  kubectl create namespace "${namespace}" >/dev/null
done

kubectl apply -f - >/dev/null <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
  namespace: fs2-data
spec:
  replicas: 1
  selector:
    matchLabels:
      app: postgres
      cnpg.io/cluster: fs2-control-db
  template:
    metadata:
      labels:
        app: postgres
        cnpg.io/cluster: fs2-control-db
    spec:
      containers:
        - name: postgres
          image: postgres:16.10-alpine3.22@sha256:029660641a0cfc575b14f336ba448fb8a75fd595d42e1fa316b9fb4378742297
          env:
            - name: POSTGRES_PASSWORD
              value: fs2test
            - name: POSTGRES_DB
              value: fs2serve
          ports:
            - containerPort: 5432
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "postgres", "-d", "fs2serve"]
            periodSeconds: 1
            failureThreshold: 60
---
apiVersion: v1
kind: Service
metadata:
  name: fs2-control-db-rw
  namespace: fs2-data
spec:
  selector:
    app: postgres
  ports:
    - port: 5432
      targetPort: 5432
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fs2-serve-catalog
  namespace: fs2-system
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 32Mi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fs2-serve-model-evidence
  namespace: fs2-system
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 32Mi
---
apiVersion: v1
kind: Pod
metadata:
  name: catalog-populator
  namespace: fs2-system
spec:
  restartPolicy: Never
  containers:
    - name: hold
      image: busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
      command: ["sh", "-c", "sleep 600"]
      volumeMounts:
        - name: catalog
          mountPath: /catalog
  volumes:
    - name: catalog
      persistentVolumeClaim:
        claimName: fs2-serve-catalog
YAML
kubectl rollout status deployment/postgres -n fs2-data --timeout=90s >/dev/null
kubectl wait --for=condition=Ready pod/catalog-populator -n fs2-system --timeout=90s >/dev/null
kubectl cp "${repo_root}/k8s-inference/catalog/runtime/." fs2-system/catalog-populator:/catalog >/dev/null
kubectl delete pod/catalog-populator -n fs2-system --wait=true >/dev/null

PYTHONPATH="${repo_root}/k8s-inference/catalog/runtime" python3 - \
  "${repo_root}" "${repo_root}/k8s-inference/catalog/runtime" "${test_dir}" <<'PY'
import base64
import hashlib
import json
import sys
from pathlib import Path

from fs2_serve_catalog.loader import load_catalog

repo_root = Path(sys.argv[1])
catalog_root = Path(sys.argv[2])
output = Path(sys.argv[3])
catalog = load_catalog(catalog_root, repo_root=repo_root)
(output / "serving-bindings.json").write_text(
    json.dumps(
        {
            "schema": "fs2-serve.nebius.ai/serving-bindings/v16",
            "catalog_digest": catalog.digest,
            "bindings": {},
        },
        sort_keys=True,
    )
    + "\n"
)
(output / "model-variant-promotions.json").write_text(
    json.dumps(
        {
            "schema": "fs2-serve.nebius.ai/model-variant-promotions/v4",
            "route_authority": "signed-live-evidence-only",
            "catalog_digest": catalog.digest,
            "attestor_policy_sha256": hashlib.sha256(b"empty-route-test-policy").hexdigest(),
            "promotions": {},
        },
        sort_keys=True,
    )
    + "\n"
)
for filename, key_id, value in (
    ("payload-keyring.json", "payload-v1", b"p" * 32),
    ("ledger-keyring.json", "ledger-v1", b"h" * 32),
    ("pepper-keyring.json", "pepper-v1", b"k" * 32),
):
    (output / filename).write_text(
        json.dumps({"active_key_id": key_id, "keys": {key_id: base64.b64encode(value).decode()}}) + "\n"
    )
attestor = b"a" * 32
attestor_id = "sha256:" + hashlib.sha256(attestor).hexdigest()
(output / "route-attestors.json").write_text(
    json.dumps({attestor_id: base64.urlsafe_b64encode(attestor).rstrip(b"=").decode()}) + "\n"
)
PY

kubectl create configmap fs2-serve-serving-bindings -n fs2-system \
  --from-file="${test_dir}/serving-bindings.json" \
  --from-file="${test_dir}/model-variant-promotions.json" >/dev/null
database_url='postgresql://postgres:fs2test@fs2-control-db-rw.fs2-data.svc.cluster.local:5432/fs2serve'
kubectl create secret generic fs2-serve-database -n fs2-system \
  --from-literal="url=${database_url}" --from-literal='ca.crt=test-only-placeholder-ca' >/dev/null
kubectl create secret generic fs2-serve-database-migrations -n fs2-system \
  --from-literal="url=${database_url}" --from-literal='ca.crt=test-only-placeholder-ca' >/dev/null
kubectl create secret generic fs2-serve-database-maintenance -n fs2-system \
  --from-literal="url=${database_url}" --from-literal='ca.crt=test-only-placeholder-ca' >/dev/null
kubectl create secret generic fs2-serve-payload-keyring -n fs2-system \
  --from-file="keyring.json=${test_dir}/payload-keyring.json" >/dev/null
kubectl create secret generic fs2-serve-ledger-hmac-keyring -n fs2-system \
  --from-file="keyring.json=${test_dir}/ledger-keyring.json" >/dev/null
kubectl create secret generic fs2-serve-token-pepper -n fs2-system \
  --from-file="keyring.json=${test_dir}/pepper-keyring.json" >/dev/null
kubectl create secret generic fs2-serve-route-attestors -n fs2-system \
  --from-file="attestors.json=${test_dir}/route-attestors.json" >/dev/null
kubectl create secret generic fs2-serve-admin -n fs2-system --from-literal="token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" >/dev/null

release_values=(
  --set "image.repository=${image_repository}"
  --set "image.digest=${candidate_digest}"
  --set image.pullPolicy=IfNotPresent
  --set "catalog.rolloutDigest=sha256:3333333333333333333333333333333333333333333333333333333333333333"
  --set config.publicBaseUrl=https://203.0.113.17
  --set config.authorizationServerUrl=https://identity.unit.test
  --set config.publicAuthorityMode=ip
  --set httpRoute.authorityMode=ip
  --set maintenance.enabled=false
  --set autoscaling.enabled=false
  --set adminConsole.enabled=true
  --set "adminConsole.image.repository=${admin_image_repository}"
  --set "adminConsole.image.digest=${admin_digest}"
  --set "adminConsole.provenance.sourceCommit=${admin_source_commit}"
  --set "adminConsole.provenance.sourceTree=${admin_source_tree}"
  --set "adminConsole.provenance.sbomSha256=${admin_sbom_sha}"
  --set-string config.schemaWaitSeconds=120
)

helm install fs2-serve "${chart}" --namespace fs2-system \
  "${release_values[@]}" \
  --wait=watcher --wait-for-jobs --rollback-on-failure --timeout 5m >/dev/null
kubectl rollout status deployment/fs2-serve-control-plane -n fs2-system --timeout=120s >/dev/null
kubectl rollout status deployment/fs2-serve-control-plane-admin-console -n fs2-system --timeout=120s >/dev/null

forward_log="${test_dir}/port-forward.log"
kubectl port-forward -n fs2-system service/fs2-serve-control-plane 18080:8080 >"${forward_log}" 2>&1 &
forward_pid=$!
for _ in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:18080/readyz >"${test_dir}/ready.json"; then
    break
  fi
  sleep 1
done
kill "${forward_pid}" >/dev/null 2>&1 || true
wait "${forward_pid}" 2>/dev/null || true
jq -e '.status == "ready" and .models == 0 and .activation.required == false' "${test_dir}/ready.json" >/dev/null

admin_forward_log="${test_dir}/admin-port-forward.log"
kubectl port-forward -n fs2-system service/fs2-serve-control-plane-admin-console 18081:8080 \
  >"${admin_forward_log}" 2>&1 &
admin_forward_pid=$!
for _ in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:18081/healthz >"${test_dir}/admin-health.txt"; then
    break
  fi
  sleep 1
done
[[ "$(cat "${test_dir}/admin-health.txt")" == ok ]]
[[ "$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:18081/admin/models)" == 200 ]]
[[ "$(curl --silent --output /dev/null --write-out '%{http_code} %{content_type}' http://127.0.0.1:18081/admin/api/v1/context)" == "404 application/problem+json" ]]
kill "${admin_forward_pid}" >/dev/null 2>&1 || true
wait "${admin_forward_pid}" 2>/dev/null || true

postgres_pod="$(kubectl get pod -n fs2-data -l app=postgres -o jsonpath='{.items[0].metadata.name}')"
[[ "$(kubectl exec -n fs2-data "${postgres_pod}" -- psql -U postgres -d fs2serve -Atc 'SELECT count(*) FROM fs2_schema_migrations')" == 9 ]]
[[ "$(kubectl exec -n fs2-data "${postgres_pod}" -- psql -U postgres -d fs2serve -Atc 'SELECT count(*) FROM (SELECT version,count(*) FROM fs2_schema_migrations GROUP BY version HAVING count(*)<>1) q')" == 0 ]]

helm upgrade fs2-serve "${chart}" --namespace fs2-system --reuse-values \
  --set-string config.workerPollSeconds=0.3 \
  --wait=watcher --wait-for-jobs --rollback-on-failure --timeout 5m >/dev/null
kubectl rollout status deployment/fs2-serve-control-plane -n fs2-system --timeout=120s >/dev/null
[[ "$(kubectl get deployment/fs2-serve-control-plane -n fs2-system -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FS2_WORKER_POLL_SECONDS")].value}')" == 0.3 ]]
[[ "$(kubectl exec -n fs2-data "${postgres_pod}" -- psql -U postgres -d fs2serve -Atc 'SELECT count(*) FROM fs2_schema_migrations')" == 9 ]]

if helm upgrade fs2-serve "${chart}" --namespace fs2-system --reuse-values \
  --set "image.digest=${failure_digest}" \
  --wait=watcher --wait-for-jobs --rollback-on-failure --cleanup-on-fail --timeout 90s \
  >/dev/null 2>&1; then
  echo "failing migration fixture unexpectedly upgraded" >&2
  exit 1
fi
[[ "$(helm status fs2-serve -n fs2-system -o json | jq -r '.info.status')" == deployed ]]
[[ "$(kubectl get deployment/fs2-serve-control-plane -n fs2-system -o jsonpath='{.spec.template.metadata.annotations.fs2\.nebius\.ai/image-digest}')" == "${candidate_digest}" ]]
[[ "$(kubectl get deployment/fs2-serve-control-plane-admin-console -n fs2-system -o jsonpath='{.spec.template.metadata.annotations.fs2\.nebius\.ai/admin-image-digest}')" == "${admin_digest}" ]]
kubectl rollout status deployment/fs2-serve-control-plane -n fs2-system --timeout=120s >/dev/null
kubectl rollout status deployment/fs2-serve-control-plane-admin-console -n fs2-system --timeout=120s >/dev/null
[[ "$(kubectl exec -n fs2-data "${postgres_pod}" -- psql -U postgres -d fs2serve -Atc 'SELECT count(*) FROM fs2_schema_migrations')" == 9 ]]

echo "helm4-gateway-lifecycle=PASS install=watcher upgrade=watcher rollback=PASS migrations=9 routes=0 admin=ready"
