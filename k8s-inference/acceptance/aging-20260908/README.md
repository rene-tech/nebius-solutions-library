# Aging worker image qualification

This bounded test qualifies immutable native worker images on the existing H100
cluster. It is not proof of public Apps, MCP, scale-to-zero or customer cold-start
behavior; those belong to the separate platform integration acceptance.

Only two temporary task-owned Pods are authorized: one zero-GPU PhenoAge worker
on the existing general CPU pool and one AltumAge worker using a single existing
reserved H100. No Services, routes, nodes, quotas or platform settings change.
Both use the original image entrypoint and native `/v1/health/ready` and
`/v1/predict` endpoints. No scientific data is real patient data.

`qualify.py` validates and creates the exact approved manifest, retains Pod UID,
status/events/logs and the GPU-performance skill's read-only environment capture,
runs `in_pod.py`, then confirms that the same owned Pod was removed. Failed
commands retain their receipts and leave their Pod available for diagnosis.
Only explicitly authorized, fresh manifests/output directories should be run.

`in_pod.py` uses the packaged fixture generator for two distinct native HTTP
predictions. PhenoAge must match the published rounded-coefficient synthetic
reference, 41.90792243377999 years. AltumAge must use CUDA and match CPU predictions
within 0.001 years for batches of 1, 2 and 8 identical synthetic samples. The
existing module benchmark runs batches 1 and 8 with 20 repetitions. Predictions
and module calls are emitted before final assertions so failure evidence remains.

`export.py` produces a field-selected, credential-free report from private raw
receipts. Each report records source/fixture hashes, exact image, measurement
boundaries and cleanup. No new model source or qualification contract is created.

## PhenoAge r01: passed

Model source: `3a3dbe0fdb258579f9d4eb1b35a2d1525f5fc0d3`.
Image: `phenoage-cpu@sha256:b6c28820576b62972437787bd06fa37f8c10b6f51de31b0f4da3cc3873bb34f1`.
Full measured receipt: [phenoage-r01.json](phenoage-r01.json).

The fresh Pod was created at 13:12:36 UTC and Ready at 13:12:37 on 2026-09-08
(one-second Kubernetes timestamp precision). The kubelet reported a 512 ms image
pull and runtime image size 53,985,973 bytes. Application construction took
0.803 ms after Python server imports. There was no artifact/weight load or GPU.

Both native HTTP predictions passed: 41.90792243377997 and 42.79962098643345 years.
Loopback request times were 1.513 ms and 1.222 ms, with zero errors. The existing
20-repetition module benchmark measured JSON validation plus prediction at
median 6.936 microseconds for one sample and 39.800 microseconds for eight;
min/max and sample throughput are retained in the JSON report. Separate
process imports plus constructor took 84.962 ms. These numbers must not be
presented as a public API or newly provisioned-node cold start.

The exact owned Pod `ee9f7119-899f-4fb7-a864-54191622b091` was removed and absence
confirmed at 13:12:51.054539 UTC. No existing runtime was modified.

## AltumAge r01: passed on H100

Full measured receipt: [altumage-r01.json](altumage-r01.json).
Exact image: `altumage-cuda@sha256:ca6352f79ecd78928e13ecda55afaf01a5df1923291b7a56a7ba9a9770c484e1`.
Actual device: H100 80GB HBM3, SM90, driver 580.159.04, Torch 2.8.0+cu128.
Model tensors were on `cuda:0`; the one GPU UUID is recorded in the report.
Weights SHA-256: `648f9d8cf8fb809e0ce9f1d46b652a936b2bb056b6d028c369ea5e2ed4a05e87`.

The Pod was created at 13:16:46 UTC and Ready at 13:18:33: **107 seconds**
at Kubernetes timestamp precision. Kubelet image pull consumed 95.529 seconds
for a reported 4,167,595,352-byte runtime image; this size is not wire traffic.
Runtime construction/imports during application startup took 3.756 seconds.
One expected connection-refused readiness probe before the server bound its
port is retained. The Pod did not restart; no public request failure is implied.

Two native requests returned different predictions, 38.22121048 and 40.12385559
years, in 22.40 and 16.10 ms. CPU/CUDA predictions for the identical 1/2/8-sample
fixtures agreed within 0.000003815 years, below the declared 0.001-year tolerance.

| Local JSON validation + prediction, 20 repetitions | CPU median | CUDA median |
| --- | ---: | ---: |
| One sample | 9.235 ms | 9.205 ms |
| Eight samples | 25.434 ms | 23.568 ms |

The CPU comparison ran in CPU mode in the same CUDA image on the same H100 host;
it is not separate qualification of a CPU-only image/deployment. These small
batches show little end-to-end GPU benefit, with JSON/scaler/host work included.
Cold image transfer dominates this fresh-node-cache startup. No snapshot restore
was performed: the runtime correctly reports GPU snapshot `not-qualified`.

The exact Pod `d9bc8eeb-31ce-4d0f-b540-a7e74093cddf` was removed and absence
confirmed at 13:19:00.433865 UTC. Both aging test clients have exited; the
customer cluster and existing apps were left unchanged.

## Catalog integration boundary

The exact image-baked AltumAge `/opt/altumage/manifest.json` was extracted from
the retained OCI image, not regenerated by the build tool. Its SHA-256 is
`321f6ceafcd2ade0d88a75ab2d492698774fb0a05b1f6c4b2a0f8ffca1acc3fa`.
The three serving inputs (`cpgs.json`, `preprocessing.npz`, `weights.pt`) total
3,567,654 bytes; the native artifact manifest binds each path, size and hash.

`fs2_serve.native_catalog.augment_native_catalog` adds declarations from
`catalog/runtime/native/*.json` to the same existing `Catalog` type after the
archival catalog/bindings have validated. It preserves all 16 archived records,
their digests, variants, qualification cohort and existing route authority.
Each native record, variant, semantic contract and artifact is content-bound;
the native declaration alone cannot publish an HTTP or MCP route.

Runtime selection remains `deployment-runtimes/*.json`, with the shared
controller responsible for deployment and live publication. PhenoAge uses an
embedded `formula` artifact with zero GPUs; AltumAge uses embedded `weights` and
one CUDA GPU. Neither receives an invented Blackwell or snapshot qualification.
The exact native HTTP request serialization is compact JSON without sorted
keys or a trailing newline, matching the retained request hashes.

Before release, 95 combined native catalog, Registry, selected-runtime,
controller, configuration and CLI tests passed. Strict typing and Ruff passed
for the new core and integration seams; scientific recipe identities remain
unchanged. Public Apps/MCP and live zero-to-one-to-zero acceptance remain a
separate next step; direct worker readiness does not qualify elasticity.

## Public Apps acceptance

`public_apps.py` is a separate, explicitly authorized acceptance campaign, not
part of the native image qualifier. It runs one client for each new aging App
in parallel, with at most one AltumAge GPU worker and one PhenoAge CPU worker.
There are four logical operations: each model gets its first original synthetic
fixture over HTTP and its second over MCP. Exact replay must retain each
operation ID; both HTTP and MCP results are checked against the original
formula or retained CPU/CUDA parity receipts. Fixture hashes must match the
native declarations before any network request is possible.

The campaign saves only these two Apps' minimum/maximum workers as zero/one
using the admin API. Existing idle, cooldown, startup budget, placement and
cache settings are preserved. It observes a real zero-container baseline,
durable accepted demand, Ready workers, published results, and automatic idle
return to zero. Pod and node UID/timestamps, image-pull events and actual device
witnesses establish whether each cold start used an existing cached node or a
new autoscaled node. Do not infer the cache boundary from cluster configuration:
r04 actually provisioned a new preemptible H100 node. No test empties the
regional registry or claims a GPU snapshot restore. The campaign also checks
exactly two new logical Runs and
Usage entries per App, scoped HTTP/MCP discovery, and revokes the two temporary
model-scoped keys in `finally`, verifying HTTP 401 afterward.

Run from `components/control-plane` with its environment. First use
`--prepare-only --release <source-commit> --artifact-root <extracted-image-artifacts>`.
After the release owner's explicit GO, add `--access-bundle <private-access-json>`
and `--output <fresh-private-cohort-directory>` instead of `--prepare-only`.
The artifact directory must contain the exact image-baked `manifest.json`,
`cpgs.json` and `preprocessing.npz`; the harness verifies their byte identities.
Neither model is resubmitted after a real failure. Request, response and
operation evidence is retained before assertions; only root may authorize a
corrective release and fresh cohort.

Twelve offline harness tests pass, including error preservation, no hidden
HTTP retry, secret redaction, thread-safe receipt creation, preservation of
accepted IDs before replay checks, and key cleanup after a failed request.

Live integration, retained failed attempts and final public acceptance are
tracked in [RELEASE.md](RELEASE.md); direct-worker figures above have different
measurement boundaries and must not replace public cold-start figures.
