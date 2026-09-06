# H100 varied-input customer readiness — 6 September 2026

All ten scientific profiles completed fresh, varied requests against the public
H100 endpoint on control-plane source `5fcc8323`. The companion catalog
permission fix was deployed before these successes. Each successful operation
passed its existing semantic validator, idempotent replay, and public download
of its output manifest plus one exact result artifact. The adjacent JSON pins
input, model, runtime, recipe, result and aggregate-receipt identities.

| Profile | Variation | Public request wall time |
| --- | --- | ---: |
| Proteina-Complexa | Three samples, seed 4, separate run name | 782.683 s |
| BoltzGen | Two concurrent 20-candidate requests, customer and bulk priorities | 812.502 / 810.991 s |
| mosaic | Two independent shards, 44-residue binder, 30 optimizer steps | 778.817 s |
| BindCraft | Two independent designs | 499.329 s |
| RFdiffusion | Four independent 96-residue designs | 418.187 s |
| ESMFold2 | 76-residue ubiquitin | 284.166 s |
| ESMFold2-Fast | 76-residue ubiquitin | 104.244 s |
| Protenix v2 | Ubiquitin, two seeds | 163.831 s |
| AlphaFold3 | Ubiquitin | 84.788 s |
| OpenFold3 | Ubiquitin, two seeds | 109.332 s |

The main campaign reached 13 overlapping admitted GPU attempts. The dedicated
BoltzGen campaign completed both concurrent service classes. Every successful
run has reconciled, application-observed GPU occupied/active/idle accounting;
this is not a claim that CUDA kernels continuously occupied those devices.
Cancellation also completed and released its owned resources.

These are one-run customer workflow timings, not p50/p95 cold-start claims.
Images and regional weights were partly warm, inputs differ between models,
and model-load/compile/restore phases are not separately exposed for every
backend. No GPU snapshot restore was requested by this campaign. Snapshot
qualification and integration remain separate acceptance gates.

## Defects reproduced and corrected

- The preceding `b44e613a` release allowed GPU execution but broke final result
  publication: model-UID companions could not read the image's public JSON
  schema. Public catalog permission normalization is live in `5fcc8323`;
  source archive mode preservation additionally prevents private release
  umasks from silently altering image inputs.
- Existing BoltzGen bounds were inconsistent with its public schema, and
  invalid parameters returned HTTP 503. The submitted fix preserves all caps
  and returns HTTP 422; its live negative test awaits the combined release.
- Two main-run BoltzGen failures were acceptance-fixture errors, not model
  failures: renamed shard IDs had no matching YAML file in the campaign tar.
  They are retained in the receipts. Both corrected concurrent requests
  succeeded, and an offline regression checks shard/archive consistency.

## Measured startup issue handed to the release owner

Several CPU preparation/finalization stages pull their model's complete GPU
image on CPU nodes, although their work requires little or no GPU runtime:

| CPU stage | Image bytes | Exact Kubernetes image pull |
| --- | ---: | ---: |
| mosaic aggregate | 4,204,444,871 | 200.072 s |
| Proteina filter | 4,373,177,599 | 151.631 s |
| Proteina analyze | 4,373,177,599 | 214.338 s |
| RFdiffusion collect | 4,154,027,189 | 164.804 s |
| ESMFold2 prepare-input | 3,815,828,990 | 162.490 s |

For example, mosaic's aggregate was scheduled immediately, pulled its image
from 21:19:35 to 21:22:55 UTC, and performed approximately one second of actual
aggregate computation. Its admin controller-phase projection reported
351.784 s as `artifact-load`; that projection includes later regular-container
image waiting and must not be described as 351 seconds of weight transfer.
The image-pull numbers above come from Kubernetes `Pulled` events, not an
estimate derived from overall wall time.

The release owner is implementing an explicit lightweight CPU-stage execution
contract. The existing map deliberately binds every stage to the model image;
silently replacing an image while reporting its old digest would be wrong.
RFdiffusion collection is the simplest case: its model container executes
`python --version`; all real finalization already lives in the collector.
Mosaic's aggregate helper imports only Python's standard library.

The saturated RFdiffusion burst additionally exposed CPU oversubscription:
PyTorch selected 64 host threads per shard inside a Pod limited to 16 cores.
Multiple shards spent minutes generating the same scratch-local IGSO3
schedules. A three-repetition, exact-array microbenchmark supports bounding
each shard's CPU thread pools to one, without changing weights, diffusion
steps, seeds or resource limits. The separate
[thread evidence](rfdiffusion-host-threads-20260906.json) retains every sample,
the varying-load limitation and the pending full-workflow acceptance gate.
This follows PyTorch's guidance to avoid oversubscribing concurrent inference
thread pools. [PyTorch CPU threading documentation](https://docs.pytorch.org/docs/2.14/notes/cpu_threading_torchscript_inference.html)

## Saturated priority and preemptible acceptance

The separate `priority-scale` campaign completed all eighteen RFdiffusion bulk
designs in 581.140 s and a higher-priority customer design in 334.211 s. It
reached 17 simultaneous GPU admissions, used reserved and preemptible flavors,
and restarted the preempted bulk shard as attempt two before delivering the
complete final result. Both operations passed public result download checks.
No node-group, queue quota or Pod resource setting was changed for the test.

Kueue preempted bulk priority -100 for customer priority 0 at 21:31:05 UTC.
However, the customer Pod did not acquire that GPU until 21:32:35: exactly the
configured 90-second termination grace. The trusted stage wrapper and
companion CLI lacked explicit PID-1 termination handling. The fix forwards
SIGTERM/SIGINT to the model process group and lets companions exit cleanly;
it preserves grace configuration and never publishes successful completion
for a cancelled process, even if a child handles termination by exiting zero.
Forty-one focused companion/staging tests passed, including real child and
grandchild signal delivery. Live cancellation/preemption latency must be
rechecked after this fix is deployed.

Customer accounting was exact/application-observed. The bulk ledger remained
reconciled but correctly marked estimated because the preempted attempt had
an inferred terminal boundary. Its 5,671.013 occupied GPU-seconds and 470 idle
GPU-seconds must not be presented as fully measured. The no-preemption varied
fleet accounting above remains exact; the receipts retain this distinction.

## Retained environment and evidence

The H100 cluster remains running for customer testing. No quota, cap, resource
limit, GPU pool size or Terraform state was changed by this worker. The parent
owns image releases, infrastructure and final integration. The adjacent JSON
references private full receipts with their SHA-256 digests; it contains no
credentials, presigned links or copied customer secrets.
