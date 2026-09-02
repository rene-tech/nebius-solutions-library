# Live KEDA elasticity acceptance

`stages/workloads/scripts/model_autoscaling_acceptance.py` is the promotion
gate for a model that is expected to scale from zero. It observes the complete
live path rather than inferring it from a Terraform plan:

```text
zero replicas/endpoints -> authenticated request accepted -> durable demand
-> KEDA Active -> HPA/Deployment >= 1 -> Pod and Node ready -> endpoint ready
-> semantic result(s) -> terminal payload purged -> zero replicas/endpoints
```

The private receipt is mode `0600`. The optional public receipt links to its
SHA-256 but hashes operation, Pod, Node, GPU, pool, capacity-block, and clock
identities. Request and response bodies are never retained. A non-streaming
operation records semantic completion latency and explicitly leaves TTFT
unavailable; it must never relabel completion time as TTFT. If an attempt
fails after admission, the harness cancels non-terminal operations,
acknowledges terminal operations to purge their payload, and makes a bounded
attempt to restore the configured floor.

## Preconditions

Apply the cluster through Terraform with the selected model in KEDA mode,
`min_replicas = 0`, and a positive `max_replicas`. The retained H100 commands
below assume the Terraform-rendered canonical Deployment and Service names.
Before running, verify that the ScaledObject is `Ready=True`, its generated HPA
targets that exact Deployment, the control plane `/readyz` response says
activation is disabled, and the model has reached zero replicas and zero ready
EndpointSlices. Do not manually scale the Deployment; KEDA owns its scale
subresource.

The request material is generated from the reviewed semantic fixtures into a
private run directory. The first and second requests are intentionally
distinct.

```bash
FS2_REPO_ROOT=/absolute/path/to/nebius-solutions-library
FS2_RUN_ROOT=/absolute/private/path/to/the/terraform-run
FS2_ENDPOINT=https://inference.example.com
FS2_KUBE_CONTEXT=k8s-inference
cd "$FS2_REPO_ROOT"
install -d -m 700 "$FS2_RUN_ROOT/elasticity"
umask 077

jq '{protocol:"openai-chat",operation:"chat",payload:.requests[0].request}' \
  k8s-inference/catalog/runtime/validators/assets/qwen3-8b.json \
  > "$FS2_RUN_ROOT/elasticity/qwen-request-1.json"
jq '{protocol:"openai-chat",operation:"chat",payload:.requests[1].request}' \
  k8s-inference/catalog/runtime/validators/assets/qwen3-8b.json \
  > "$FS2_RUN_ROOT/elasticity/qwen-request-2.json"

jq '{protocol:"native",operation:"generate-media",payload:.requests[0].request}' \
  k8s-inference/catalog/runtime/validators/assets/cosmos3-nano.json \
  > "$FS2_RUN_ROOT/elasticity/cosmos-request-1.json"
jq '{protocol:"native",operation:"generate-media",payload:.requests[1].request}' \
  k8s-inference/catalog/runtime/validators/assets/cosmos3-nano.json \
  > "$FS2_RUN_ROOT/elasticity/cosmos-request-2.json"
```

Run Qwen after its content-addressed shared cache has been populated once:

```bash
python3 k8s-inference/stages/workloads/scripts/model_autoscaling_acceptance.py \
  --kubeconfig "$FS2_RUN_ROOT/kubeconfig" \
  --context "$FS2_KUBE_CONTEXT" \
  --endpoint "$FS2_ENDPOINT" \
  --tls-mode verified \
  --token-file "$FS2_RUN_ROOT/operator.pat" \
  --request-file "$FS2_RUN_ROOT/elasticity/qwen-request-1.json" \
  --second-request-file "$FS2_RUN_ROOT/elasticity/qwen-request-2.json" \
  --semantic-call-count 2 \
  --capture-runtime-identity \
  --require-cache-outcome cache-hit \
  --optimization-matrix "$FS2_REPO_ROOT/k8s-inference/models/cold-start/cold-start-optimization-matrix.json" \
  --benchmark-mechanism shared-cache \
  --namespace fs2-models \
  --model-id qwen3-8b \
  --deployment qwen3-8b-b300 \
  --service qwen3-8b-b300 \
  --expected-floor 0 \
  --cooldown-seconds 300 \
  --timeout-seconds 7200 \
  --scale-down-timeout-seconds 1200 \
  --cleanup-timeout-seconds 1200 \
  --evidence-file "$FS2_RUN_ROOT/elasticity/qwen-private.json" \
  --public-evidence-file "$FS2_RUN_ROOT/elasticity/qwen-public.json"
```

Run Cosmos after its content-addressed shared cache has been populated once:

```bash
python3 k8s-inference/stages/workloads/scripts/model_autoscaling_acceptance.py \
  --kubeconfig "$FS2_RUN_ROOT/kubeconfig" \
  --context "$FS2_KUBE_CONTEXT" \
  --endpoint "$FS2_ENDPOINT" \
  --tls-mode verified \
  --token-file "$FS2_RUN_ROOT/operator.pat" \
  --request-file "$FS2_RUN_ROOT/elasticity/cosmos-request-1.json" \
  --second-request-file "$FS2_RUN_ROOT/elasticity/cosmos-request-2.json" \
  --semantic-call-count 2 \
  --capture-runtime-identity \
  --require-cache-outcome cache-hit \
  --optimization-matrix "$FS2_REPO_ROOT/k8s-inference/models/cold-start/cold-start-optimization-matrix.json" \
  --benchmark-mechanism shared-cache \
  --namespace fs2-models \
  --model-id cosmos3-nano \
  --deployment cosmos3-nano \
  --service cosmos3-nano \
  --expected-floor 0 \
  --cooldown-seconds 300 \
  --timeout-seconds 7200 \
  --scale-down-timeout-seconds 1200 \
  --cleanup-timeout-seconds 1200 \
  --evidence-file "$FS2_RUN_ROOT/elasticity/cosmos-private.json" \
  --public-evidence-file "$FS2_RUN_ROOT/elasticity/cosmos-public.json"
```

## Promotion gate

Do not add a model to `qualification.elasticity_qualified_models` merely
because a Pod once became Ready. For each exact model revision, runtime image,
artifact digest, accelerator class, and pool tuple, require a `PASS` private
and public receipt with all of these properties:

- initial and final replicas, ready replicas, and endpoints are zero;
- `scaledobject_active`, `hpa_desired_one`, `deployment_one`, and
  `endpoint_ready` are all true;
- the durable operation succeeded and both distinct semantic calls completed;
- runtime identity binds the exact image and model-content annotations;
- localization is a `cache-hit` or `cache-hit-after-wait` when shared-cache
  qualification is claimed;
- cleanup is `PASS`, all terminal payloads were acknowledged, and the floor
  was restored; and
- the SHA-256 of the private receipt equals
  `source.private_receipt_sha256` in the public receipt.

After independent review, retain both receipts under
`catalog/profiles/evidence/`, add their content digests to a reviewed
elasticity qualification receipt, add only the passing model IDs to
`qualification.elasticity_qualified_models` in
`components/control-plane/contracts/all-models-live-services.json`, then run:

```bash
python3 k8s-inference/components/control-plane/scripts/render_all_models_live.py
python3 -m pytest -q k8s-inference/components/control-plane/tests/test_all_models_live_release.py
```

If either attempt fails, keep `elasticity_qualified=false`, retain the failure
receipt for diagnosis, and correct the Terraform/KEDA/runtime path before a
fresh attempt. A receipt is tuple-specific; moving to another accelerator,
runtime digest, artifact revision, cache tier, or placement pool requires new
live evidence.
