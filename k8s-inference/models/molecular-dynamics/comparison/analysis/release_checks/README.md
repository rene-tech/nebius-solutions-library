# Bounded large-artifact release acceptance

This helper tests only the two explicitly authorized, existing LAMMPS customer
artifacts listed in its source. It does not run simulations, upload artifacts,
create keys, alter limits or change cluster configuration. TLS verification stays
enabled. Each cohort intentionally closes one stream after consuming 1 MiB,
then streams and discards both complete artifacts concurrently while computing
SHA-256. Read-only readiness/operation and Pod/memory observations run alongside.
An explicitly authorized short-lived operator session inspects only matching
tenant/principal/target-artifact metadata and is closed afterward. No credentials,
native artifact bytes, foreign requests or full debug headers/bodies are exported.

## Actual Helm 227 result

Both authorized cohorts passed on 2026-09-23. Exact control-plane index:
`sha256:719ec336ef93e582f3031735dba974b61ea97f1c4ce47e630fe18eae9e9cd77c`.
All three ready Pod identities were unchanged and every restart delta was zero;
their memory limits remained 2 GiB. No deployment or limit change was made by
this helper. Native scientific inputs/outputs and the frozen portable analysis
package were untouched.

Final receipt:
`/home/tux/fs2-alanine-analysis-20260923/large-artifact-release-04/receipt.json`
SHA256 `0c7d86f90095a26722f04f38a54547e20146fb4314b757ed57980d2f76144b56`.

| Measurement | Cohort 1 | Cohort 2 |
|---|---:|---:|
| Full concurrent download start (UTC) | 19:30:26 | 19:41:14 |
| 465,681,353-byte artifact wall time | 13.780 s | 16.301 s |
| 321,192,501-byte artifact wall time | 9.852 s | 9.775 s |
| Full bytes verified | 786,873,854 | 786,873,854 |
| Passed readiness/operation polls | 20 / 20 | 22 / 22 |
| Maximum observed poll latency | 0.672 s | 0.617 s |
| Correct owned artifact-reference rows | 3 / 3 | 3 / 3 |

These are HTTP wall measurements for this bounded acceptance, not GPU throughput
or general storage performance claims. All four full responses passed both local
size/hash verification and independent operator `artifact_reference` metadata:
`complete=true`, `verified=true`, correct full size and observed SHA-256.
Both intentionally partial responses have `complete=false`, `verified=false`.
The client consumed 1,048,576 bytes while the server observed 25,165,824 bytes
before disconnect; gateway/transport buffering makes those different boundaries.
They are retained separately, not forced to agree. All six rows record
`disconnected=true`, including complete verified responses, so that transport
flag alone is not treated as evidence of an incomplete artifact. Each retained
debug body contains only 163 characters of reference metadata.

The gateway rewrote the initial client `X-Request-ID`, and the backend assigned
its own request ID. Original failed helper/correlation receipts 01–03 remain
preserved. Cohort 1 was reconciled from the unique same-owner, exact-artifact
server start inside each recorded client request interval; no completed download
was repeated. Cohort 2 used a distinct `X-FS2-Qualification-ID`, which survived
ingress and matched uniquely in addition to owner/path/time. Ambiguous matches
are rejected. New qualified requests cannot silently fall back to time matching.

## Sampled memory, not a continuous peak

All values below are actual metrics-server container-memory samples in bytes.
Full timestamps and sampling windows are retained in the receipt.

| Control-plane Pod suffix | Before cohort 2 | Maximum sampled | Fresh after sample |
|---|---:|---:|---:|
| `7pn7r` | 248,627,200 | 276,934,656 | 248,778,752 |
| `hgksh` | 248,184,832 | 248,709,120 | 247,959,552 |
| `jhr7n` | 248,397,824 | 275,673,088 | 249,069,568 |

The maximum samples have native timestamps 19:41:24, 19:41:19 and 19:41:18 UTC;
fresh after samples are 19:41:56, 19:41:49 and 19:41:50 UTC. Cohort 1's initial
samples lagged its transfers (native timestamps 19:30:01–19:30:22). Its later
reconciliation includes explicitly labeled later after-samples, not an invented
in-transfer maximum. These observations establish bounded sampled memory and
no restarts in the tested intervals, not a continuous peak-memory measurement.

Predeployment evidence for only the exact three old Pods is retained separately:
`/home/tux/fs2-alanine-analysis-20260923/large-artifact-predeploy-pods-01.json`.
It records the prior OOMKilled exit-137 states and restart counts 2, 2 and 1
under the same 2 GiB limits. No logs or credentials were included.

## Local tests and rerun boundary

Run `python3 -m unittest discover -s <this-directory> -p 'test_*.py' -v`.
Fourteen explicitly synthetic transport/metadata tests pass, including corrupted
identity/digest, partial streams, ingress request-ID rewriting, ambiguous/foreign
debug records, required qualification headers and restarted/replaced Pods.
Synthetic tests are not the actual service acceptance above.

Do not run this helper against another deployment or artifact set without fresh
scope and deployment approval. A failure stops further cohorts. The
`--resume-receipt` option rechecks already successful transfer metadata and runs
only the unfinished second cohort; it never automatically repeats the first
pair. Receipt directories must be new and existing failures are not overwritten.
