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
