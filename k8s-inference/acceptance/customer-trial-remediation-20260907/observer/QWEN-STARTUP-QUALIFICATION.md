# Standalone Qwen burst startup qualification

Prepared 2026-09-08; **root must explicitly authorize START after Terraform
release and RF recovery**. This traffic is separate from the unchanged
25-second ordinary sampler and the unchanged scientific cohorts. Do not run
either campaign concurrently with this dedicated check.

Use `experience/qwen_startup_qualification.py` with a fresh private output
directory, the existing private access bundle, explicit H100 kubeconfig and
context `k8s-inference-h100`. The bounded defaults are three simultaneous
public clients, at most 180 distinct requests, a 900-second new-admission
window, 300-second individual operation deadline and 90-second final
observation. Each request is submitted once; failures stop new admissions,
are retained unchanged, and are never retried, cancelled or hidden.

The fixture asks for harmless educational prose on libraries, the water cycle,
or telescopes, with a distinct marker per request and a 1024-token limit.
The semantic smoke test requires that request's marker and at least 100 words;
it is not a factual-model-quality benchmark. Longer ordinary generations keep
real queue/active demand visible while a new node/image initializes, without
changing autoscaling policy, request/resource limits, hot floors, replicas,
runtime images, checkpoint recipes or drivers. The helper refuses an already
existing burst Pod rather than calling a warm Pod a new cold start.

Pod status and counters are observed every five seconds. No extra inference is
sent directly to a Pod. Existing Kubernetes read-only Pod proxy access to
`/metrics` works on this cluster; Prometheus currently has no scraped
`vllm:request_success_total` series, so its absence is not represented as zero.
The proxy supplies exact per-Pod success counters for `stop` and `length`,
excluding abort/error/repetition. A restored process can carry old counters:
the first accessible **Ready** value is its baseline, never an assumed zero.
The helper stops new admission after a new Ready burst Pod's observed counter
has increased by at least two, then finishes already accepted requests and
retains the recovery window. It always reports `evidence-collected-review-required`;
it cannot independently declare a snapshot or customer-acceptance pass.

## Root/observer evidence gate

1. Record exact deployed source/image and unchanged model/scaler policy, T0
   before the first public submission, baseline hot Pod UID and no burst Pod.
2. Retain the new burst Pod UID/node and lifecycle: node pending, image pulling,
   init/localization, main container, actual CUDA restore, Ready, and first
   useful completion. Report image sizes as runtime-reported sizes, not network
   bytes. A cache hit is not a fresh-node pull. No preparation shifts outside T0.
3. Show startup retention greater than zero while the initial pull exceeds the
   old 30-second idle cooldown, and no premature deletion. Fixed-hot UID/Ready,
   public route publication and existing replica ceilings remain unchanged.
4. Read the retained new-Pod runtime logs and actual marker. A configured
   snapshot option, a ready Pod or a `normal-load-fallback` marker does not prove
   GPU restoration. The actual marker must identify `cuda-criu-restored` for
   this runtime attempt. Preserve missing/unavailable logs explicitly.
5. Prefer matching the two distinct successful public operation IDs to this
   Pod's request logs. The control plane forwards
   `x-request-id=<operation UUID>:<attempt>` and `x-fs2-operation-id`; public
   receipts also retain upstream response IDs, unique request/response hashes,
   complete outputs and statuses. Do not claim that headers necessarily appear
   in the runtime's existing logging configuration.
6. If exact IDs are absent, the per-Pod counter increment is narrower proof of
   actual useful burst compute. Correlate it with this isolated traffic window,
   all unique successful public results and absence of unrelated test traffic;
   state that attribution is aggregate, not an exact per-operation backend
   binding. If counters remain unchanged, inspect existing routing/keepalive
   behavior before claiming public burst use. A new client wave may be tried
   only within the original request/concurrency/window bounds, never by
   switching services or mutating policy mid-test.
7. After requests finish, retain actual natural idle/scale-down and no task-owned
   nonterminal operations. An operation without a terminal receipt remains
   unknown/nonterminal, not cleaned up. Do not force capacity down. Root owns
   further remediation and the two unchanged clean customer cohorts.

Keep all raw requests, outputs, Pod logs, operation IDs and access details in
the private release directory. Export redacted conclusions and hashes only.
The original r02 154.835-second cancelled image pull remains negative evidence.
