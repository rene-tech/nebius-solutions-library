# Live public-surface acceptance

`run_live_surface_acceptance.py` re-accepts one deployed `k8s-inference`
stack from the outside, the way a customer or operator meets it: the public
HTTPS edge with a normally trusted certificate, the authenticated admin
backend, both MCP catalogs, the HTTP scientific discovery route, the
OpenAI-compatible catalog, and one real chat completion. It then compares the
Kubernetes release objects and the Kueue scheduling surface against the exact
source commit and immutable image digests the operator expects.

Its output is a value-suppressed receipt. Credentials are read from the
owner-only `inference-stack output` bundle and used only in memory; the
receipt contains identities, counts, status codes, and booleans and never a
bearer token, cookie, presigned handle, or generated model text. The runner
refuses to write a receipt that would contain a bundle credential value.

## Inputs

| Argument | Meaning |
| --- | --- |
| `--bundle` | Mode-`0600` regular file written from `./inference-stack output`. A symlink or a group-readable file is rejected before any probe. |
| `--kubeconfig`, `--context` | The deployed cluster, read only through `kubectl get ... -o json`. |
| `--expectations` | Deployment expectations JSON (schema `fs2-serve.nebius.ai/live-surface-expectations/v1`). |
| `--source-commit` | Exact deployed Git commit, recorded in the receipt target. |
| `--control-plane-digest`, `--admin-console-digest` | Exact OCI digests every release Deployment and the GPU observer DaemonSet must run. |
| `--receipt` | Optional mode-`0600` output path. Existing receipts are not replaced unless `--overwrite` is explicit. |

[`expectations/h100-retained.json`](expectations/h100-retained.json)
describes the retained eu-north1 H100 deployment: two general serving models,
ten qualified scientific profiles, the two licensed profiles that the general
token must not see, nine observability launches, the Kueue queues, flavors,
and priority classes, and the semantic chat probe. Another deployment supplies
its own expectations file; the schema is validated before the network is
touched.

## Checks

| Check | Passes when |
| --- | --- |
| `terraform_output_bundle` | Owner-only mode `0600`, complete cluster identity, endpoints and credentials, shared MCP/inference PAT, and a distinct scientific PAT. |
| `tls_normal_trust` | The public host completes a TLS 1.3 handshake under the default trust store with at least one subject alternative name. |
| `public_pages` | Admin portal, `/readyz`, Grafana API health, Alertmanager and Tempo explore all return 200. |
| `kubernetes_release` | Gateway, model controller, and admin console Deployments are fully rolled out on the exact digests; the GPU observer DaemonSet is complete on the control-plane digest. |
| `terraform_cluster_contract` | The Terraform-owned live contract matches the bundle's cluster, project, region and context and carries the requested exact source commit. |
| `kueue` | Exactly the expected cluster queues and resource flavors exist and are active, every local queue is active, and the scientific priority classes exist. |
| `admin_backend_fully_qualified` | Session cookie round trip, server-authoritative context, exactly the expected general models with no unknown state, every scientific profile `qualified`, all observability launches enabled, and the node scaler available. |
| `general_mcp_scoped_catalog` | The general PAT sees the general models and the scientific catalog minus the licensed profiles, with private zero-TTL discovery on the expected protocol version. |
| `scientific_mcp_complete_catalog` | The scientific PAT sees no general model and the complete scientific catalog. |
| `openai_catalog` | `GET /v1/models` lists exactly the OpenAI-listed models, enabled, and each model's `gpu_class` equals the accelerator class the admin identity reports for it. |
| `http_scientific_discovery` | `GET /v1/scientific-models` mirrors both MCP catalogs per token and every row carries operations, service classes, a parameter schema, and an execution identity. |
| `openai_chat_semantic` | One real chat completion returns the requested marker with a positive completion token count and a `succeeded` operation. When the gateway bounds its synchronous wait, the runner follows the operation to its durable result. |

The `openai_catalog` cross-check exists because a dynamic `ModelDeployment`
keeps its canonical catalog record, whose accelerator class names the original
qualification. The public listing must name the pool the controller admitted
the model on, exactly like the admin identity already did.

## Run

Use the control-plane environment; it provides `httpx`, `httpx2` and `mcp`.
Print the bundle only into an owner-only file:

```bash
umask 077
NEBIUS_PROFILE=sandbox ./inference-stack output --var-file /private/terraform.tfvars \
  --run-root /private/run > /private/run/access-bundle.json

uv run --project components/control-plane python acceptance/live-surface/run_live_surface_acceptance.py \
  --bundle /private/run/access-bundle.json \
  --kubeconfig /private/run/kubeconfig \
  --context k8s-inference-h100 \
  --expectations acceptance/live-surface/expectations/h100-retained.json \
  --source-commit "$(git rev-parse HEAD)" \
  --control-plane-digest sha256:... \
  --admin-console-digest sha256:... \
  --receipt /private/run/acceptance/live-surface-$(git rev-parse --short HEAD).json
```

The command exits `0` only when every completed check passes, `1` on a failed
check or an unexpected collection failure, and `2` when an input is malformed.
A receipt is written after a complete evaluation; a transport or `kubectl`
failure may stop collection before a receipt can be assembled. The chat probe
consumes one short completion on the hot general model and no scientific GPU
time.

Run the offline tests with:

```bash
acceptance/live-surface/run_checks.sh
```
