# Customer job reliability recovery — 7 October 2026

Scope: fix the observed checkpoint-transfer failure, restore the failed database
replica and GPU monitoring, add owner-approved important-only email, qualify the
release internally, and recover the failed Lynx GROMACS operation. This is not a
new whole-platform/customer-workbench qualification or a new multi-day soak.

Target: `project-e00rene`, eu-north1, cluster
`mk8scluster-e00j5z9te7x5dd9g6a`; public API `https://89.169.99.188`.
Private raw evidence: `/home/tux/secure-handoff/fs2-reliability-recovery-20261007`.
Task: `fs2-customer-job-reliability-recovery-r20261007` in NIM Fast Start Platform.

## Incident and fix

Lynx operation `5a6cba05-2de1-43e5-8dc7-a29d5a222d76` failed on October 6 at
12:09:36 UTC with `artifact_collector_failed`, after about 5 hours 47 minutes.
Loki retained `httpx.RemoteProtocolError: Server disconnected without sending a
response` from batch upload finalization at 12:09:04.922786 UTC. The upload code
caught only connection-establishment errors; a lost response escaped the retry
loop and stopped the workflow. The underlying reason for that TCP disconnect
was not established. It was not the configured fourteen-day timeout.

The last committed checkpoint was generation 67, step 76,933,000 = 153.866 ns of
the 1,000 ns target. All 67 native segments completed with exit code zero.
Checkpoint manifest `fe94ebef-7f35-416f-8777-9a7f8a0a3b52`, SHA-256
`5fcd7c6e682cf7cbeaac01c2fc66e5f9bb32e6515e51e860fcf6b865e5d88d26`, retained
1,070 files. Original inputs and bucket remained available.

`WorkloadArtifactHttpClient._upload_request` now retries `httpx.TransportError`
within the existing five-attempt/backoff budget. Upload identities and bytes are
stable across retries, including when the server accepted a finalize operation
but its response was lost. Auth/digest/permanent HTTP errors remain fail-fast.
This is **native GROMACS checkpoint recovery**, not CUDA/GPU process snapshotting.

## Exact release

| Component | Identity |
| --- | --- |
| Retry source | `eefdd8d47c06cca26a0605029e59dece840556e2` |
| Final API/metric source | `4b71fba875e39f4692dd88b5c7d4c6c6a9f08b3a` |
| API, model controller, maintenance | `sha256:a49f9835c5c57571d78db3d3dc2e7cc1334d4c341ed6746c55dfbff62dc40739` |
| Qualified scientific collector | `sha256:22ee25fcd162b9f0e9fe62fa2ab3ef2b6faed097cebbdd53d8898118c3579117` |
| Unchanged GROMACS worker | `sha256:ca863f44c7d17c8096267ec43939cda8b9546f0d3b11440e62bcf9149bc94a1e` |
| Unchanged scVI training worker | `sha256:063877787f8c1c1aef28887242389449b871e1d48c74edc07c267bccee340246` |

Control-plane images live in
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane`.
Build receipts retain source trees, OCI archive/manifest hashes, SPDX SBOM and
SLSA provenance. Image promotion preserved digest/attestation identities.

The guarded release helper compare-and-tests live Pod templates, changes only
API/init/collector image references and the image-digest annotation, and saves
exact rollback patches. It preserves WhiteLab/scVI serving maps, workbench
registrations, scheduling, keys, buckets, Services, Gateway and admin UI. No
customer client or unrelated benchmark Pod was replaced.

The private H100 root tfvars now records the final API digest, the independently
qualified `deployment.scientific_batch.tools_image`, and approved email settings.
A structural comparison proves only those three paths changed. The pre-change
file is retained privately. No broad Terraform apply was performed against
older generated stage inputs. Root, foundation and workloads validate locally;
this does not claim an end-to-end fresh-cluster replay was performed here.

## Tests and measured bounds

The retry/metrics regression suite passed 96 tests, including actual PostgreSQL
durable terminal-fact projection. Separate image-patch/Helm/rule tests verify
unchanged sibling configuration, correct Prometheus rule selection, important-only
delivery and internal-tenant exclusions. Root/foundation/workloads Terraform
validation and Helm lint pass.

Real GPU qualification reuses the existing `system/qa` key, concurrency limit two,
shared internal bucket, and the previously authorized scientific input copy.
Each fixture has a deliberately bounded 120-second bootstrap ending in expected
`WORKFLOW_TIME_LIMIT_EXCEEDED`, then resumes through the real public REST or MCP
API to a finite 2-ns target. The science comparison allows only that finite test
horizon; successful launch alone is not the gate.

| Cohort | Source operation | Resumed operation | Path | Result |
| --- | --- | --- | --- | --- |
| A (rollout resilience) | `1d8736d7-d814-4560-9f6c-c9143a752610` | `8eab500d-758b-4de6-a556-1bfb263b2f5e` | REST | Passed; 204.196 delivered ns/day |
| B (final release) | `29e443a6-681d-4262-8a84-36ebc24b1099` | `88a448b6-6249-4563-aa28-f31e15c10f68` | MCP | Passed; 199.804 delivered ns/day |
| C (final release) | `2c210b5e-0e81-4c00-bab0-6689e957800f` | `78e9b9a3-df40-4314-a553-08d8c19e9739` | REST | Running at this intermediate evidence point |

A spans the API-only metric rollout; it is not counted as an unchanged final
release cohort. B and C use final API `a49f9835` and collector `22ee25fc`.
Every terminal cohort checks the exact restart step, native flags, semantic
validation, all file SHA-256/size pairs, unchanged 305-file scientific history,
idempotent replay and independently exported S3 objects. A and B each verified
335 files / 291 unique bucket objects. Their three native segments all exit zero.

These bounded recovery cohorts measure performance; their helper's zero minimum
rate is **not a 200 ns/day performance gate**. In particular, B's 199.804 must not
be rounded into a claim of meeting a strict 200 minimum. The actual customer
observation helper separately enforces 200 delivered ns/day over new checkpoints.

All internal GPU work uses one GPU, 8 CPUs and 16 GiB RAM per attempt on existing
four-L40S node `computeinstance-e00xwjv9khjp8fhp3v`, regular capacity. No new node,
reservation, quota or customer concurrency change was needed. Existing unrelated
`bgperf-20261007-*` jobs remain untouched.

### Real accepted-response-loss injection

`live_upload_fault.py` runs only inside an internal collector. It forwards actual
upload bytes to the live API/object store, discards one **successful** finalize
response locally, then checks that retrying the same payload creates one artifact.
No global proxy, network policy or customer attempt is modified.

- Rollout collector attempt `07afde41-3ff3-5e28-b292-b9ffbd95ed76`: pass, two
  finalizes, artifact `b5b078a3-85f4-4094-b1ae-a72c32c7b774`.
- Final-release attempt `ea77a296-d64e-5760-ab9e-4ecf23d805ce`: pass, two
  finalizes, artifact `7d81bcb5-49a6-44fa-ae46-75e3404bceb9`.

The probe bytes are explicitly internal transport evidence, not fabricated
molecular checkpoint data. The native continuation and all native outputs are
validated separately.

## Monitoring and important email

`fs2-dcgm-exporter` Helm revision 6 is healthy on 25/25 Pods after replacing
unsupported profiling-counter watches with portable device telemetry. Fresh
Prometheus device series include 28 H100, 2 H200 and 12 L40S GPUs. This is an
observed telemetry count, not guaranteed allocatable cloud capacity. No driver
reset or scientific workload interruption was used. The persistent values are
`stages/workloads/values/dcgm-portable-metrics.yaml`; advanced profiling counters
need separate SKU/driver qualification.

The owner explicitly chose important-only email to `rene@nebius.com` instead of
Slack. The `fs2-important-alerts` chart is deployed, four rules are loaded/healthy,
and a labelled test notification was accepted by SMTP (15 → 16 successful email
notifications, zero send failures). Pre-existing website notifications account
for the earlier counter; they were not overwritten or disabled. Inbox receipt
cannot be queried with the existing send-only credential.

See [thresholds, Terraform setup and limits](../../docs/important-customer-alerts.md).
No periodic agent or customer auto-restart is installed.

## Database and actual customer recovery

The failed database replica is being safely recloned while primary 1 and replica
3 stay online. Old data is retained; only replica 2's PVC was expanded to 200 GiB.
See [exact repair and pre-existing resilience limitations](DATABASE-RECOVERY.md).
Final three-instance and customer-progress evidence will replace this intermediate
status after the running checks complete. No customer recovery was submitted at
this evidence point; the source remains failed and immutable.

## Negative results and rollback

- Initial fault-injection attempts chose unavailable/read-only temporary paths;
  the final run uses the mounted `/mnt/fs2-scientific` workspace and passes.
- The first DCGM rollout wait timed out midway; an idempotent wait/upgrade completed
  revision 6 with 25/25 Ready. The intermediate timeout is not reported as a pass.
- The first manually timestamped email test was suppressed before delivery;
  the separate no-timestamp delivery proof sent exactly one labelled test.
- Temporary WhiteLab qualification key was already explicitly revoked at its
  earlier closeout; a read-only probe received 401. The persistent system/qa key
  still works and sees scVI. An unrelated-principal historic operation returned
  404; no permissions were broadened to bypass ownership. Those probes do not
  qualify new scVI training or customer LibreChat behavior.
- A verification command initially omitted its `jsonschema` dependency; the
  corrected dependency set independently verified exported objects.
- Local workload-stage init initially used read-only locking in a fresh checkout;
  normal backend-disabled initialization followed by validation passed. No cloud
  resources were changed by initialization.
- Pytest initially warned about old unrelated root-owned temporary sockets;
  task-specific temporary test directories pass without those warnings. No other
  task's files or containers were removed.

Protected `release/` and `release-alerts/` directories contain compare-and-test
rollback patches; do not replay one after another owner changes the template.
Rolling back the retry fix reintroduces the failure and does not change collectors
already running in scientific Jobs. Prefer keeping the fix. Alert delivery can
be disabled by the owning chart; never delete shared website routes/receivers.
The database recovery is not reversed by discarding new or retained old data.
