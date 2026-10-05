# Coherent schema-38 reader release

Source-only proposal for the existing H100 cluster, 5 October 2026. Nothing in
this directory applies Kubernetes changes. The parent release manager owns
review, migration, deployment and customer-path acceptance.

## Why main-image-only deployment is unsafe

At 18:21 UTC the live API main image was `64c5c77d4eb6…`, but its `wait-schema`
init image was `fb6d32098a2e…`. Both package 37 migrations. The init checks the
**exact** ordered migration ledger and hashes: once migration 38 is recorded,
that old init cannot start. An already-running API does not rerun the init.
The new init waits at ledger 37, so do not start a candidate rollout and wait
for readiness before advancing the ledger.

The online index was already built without advancing the ledger. Parent proof:
`secure-handoff/fs2-lynx-longrun-20261005/claim-index-verified/verification.json`
(18:03:59 UTC, exact definition, `indisvalid=true`, `indisready=true`). This is
not permission to rerun an online build or insert migration ledger rows manually.

## Inventory and ownership

Read-only inventory includes Deployments, CronJobs, Jobs, DaemonSets and
StatefulSets across namespaces. The 18:21 capture contained 109 Scientific AI
objects. Secret names/key references are recorded, never their values, request
bodies or arbitrary shell arguments. Unknown DB consumers or sidecars fail the
proposal rather than being silently omitted.

| Consumer | Database/startup behavior | Release action |
| --- | --- | --- |
| `fs2-system/fs2-serve-control-plane` | Shared DB; strict `wait-schema` init | Update main, init and future tools image atomically while paused |
| `…-model-controller` | Shared DB; direct asyncpg connection, no strict ledger waiter | Coherent candidate image; preserve envelope and bundles |
| `…-maintenance` CronJob | Shared DB; direct connection, no strict waiter | Suspend future Jobs, stage image, let existing Job finish; restore exact original suspend state |
| `fs2-mindeval-workshop` | Shared DB, independent workshop `schema.sql` and entrypoint | Preserve its `ee6eea4a…` image and two replicas |
| `…-benchmarks` | HTTP API client, no DB credential | Preserve `d534cdad…` and four replicas |
| `fs2-mindeval-gateway` | SQLite, no main PostgreSQL ledger | Preserve |
| `…-gpu-observer` DaemonSet | Kubernetes/node checkpoint reader, no DB | Preserve |
| Scientific Jobs/init/collectors | Frozen HTTP capability clients, no direct DB | Preserve all existing Jobs and their image bindings |
| Online-index/completed maintenance Jobs | One-shot DB consumers, already complete | Retain evidence; do not recreate them |
| Model bootstrap/cache Jobs | HTTP or cache work, no DB ledger | Preserve |
| `fs2-stt-qualification-20260927/*` | Separate `stt-control-db-rw` PostgreSQL server | Exclude from this migration; preserve |

The actual shared server is `fs2-control-db-rw.fs2-data.svc.cluster.local:5432`,
database `fs2serve`. STT uses `stt-control-db-rw.fs2-stt-qualification-20260927.svc`,
not that server. No currently recurring strict bootstrap-access Job was found.
The chart's future `migrate` and `bootstrap-access` Jobs must use the same new
image; the chart already derives these and API init from the common image.

The active customer Job `fs2-models/fs2-workflow-mas1-20e-a1-7d7046f29109` and
its frozen `f02e712a…` companions remain untouched. Companions select their
HTTP-only entrypoint before loading the API/database module.

## Reviewed order (operator, not automated by this helper)

1. Finish the current internal acceptance cohort. Recheck public readiness and
   active customer continuity. Verify candidate provenance and the full embedded
   `/opt/fs2/catalog` against the current published catalog. **No new RDMA profile
   or execution/scheduling map is published in this reader-only phase.**
2. Generate a fresh proposal with the exact immutable candidate. Review UID,
   resourceVersion and every permitted patch. Suspend future maintenance Jobs,
   stage their candidate image, and allow an already-running Job to finish.
3. Atomically set API `paused=true` and stage **main + wait-schema init + future
   scientific tools**. Stage the model-controller image without changing its
   ConfigMaps. Do not patch any frozen scientific Job. Do not wait for API
   rollout while it is paused. Old API processes continue using ledger 37.
4. Use the emitted migration Job with the existing
   `fs2-serve-control-plane-migration` ServiceAccount and
   `fs2-serve-database-migrations` Secret/CA. It checks the candidate's exact
   38-migration manifest, exact preceding 37 ledger entries and the prebuilt
   index. It closes the read-only preflight connection, then calls the existing
   migrator, which owns its advisory transaction lock. Only the release manager
   may perform DDL during this transition. An already exact-38 ledger is a
   verified no-op. Any other ledger/index state stops the Job.
5. Require Job success, exact ledger 38 and valid/ready index. Generate fresh-RV
   restore proposals with `--restore-from … --restore-target api`. Restore **only API's original paused
   state first**. If it was absent, remove the field; if true, preserve true and
   do not claim a rollout occurred. Do not unpause unrelated suspended work.
6. Wait for all three API readers and both controller readers to be ready on
   the candidate, with no old API readers running or terminating, and verify
   every API `wait-schema` init uses the candidate. Preserve API strategy
   `maxSurge=0/maxUnavailable=1`; no resource or admission-limit increase.
   Check public authenticated discovery/readiness, active customer continuity,
   fresh live queues and the parent's historical-metrics gate. The latter
   requires repeated successful accounting refreshes, not just bounded failures.
7. Only after the reader barrier, restore maintenance's original suspend flag
   using `--restore-from … --restore-target maintenance` from a **new fresh read**
   and verify a candidate-image maintenance Job.
   Existing `suspend=false` returns to false; absent stays absent; true stays
   true. Current initial API `paused` is absent and maintenance `suspend=false`.
8. Only after these gates, publish RDMA profiles plus matching execution and
   scheduling maps coherently. Old `64c5…` readers cannot decode those additive
   frozen fields. Continue the real REST/MCP/restart acceptance on the final
   release, not on an assumed-compatible mix.

Changing the migration ledger back or `kubectl rollout undo` to the old
37-migration init is **not** a valid rollback. Prefer a forward fix with the
reviewed 38-migration image set. Before a later Helm/Terraform redeploy, persist
the coherent image binding/release contract in the owning release overlay;
do not replay the stale September inputs that still bind older images/maps.
This proposal does not reconcile those broader infrastructure inputs.

## Preparing proposals

From the repository root (the image below is the intermediate reader, not a
claim that it was deployed):

```sh
k8s-inference/components/control-plane/.venv/bin/python \
  k8s-inference/acceptance/schema38-rollout-20261005/prepare_rollout.py \
  --candidate-image cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:80b39cb26afd89d84a5daa9a48c8c29a62bcbe1ba3fd78b901d3f2f515aada74 \
  --output /operator-private/new-review.json
```

The helper only runs `kubectl get`; it deliberately has no `--apply`. Output
contains an ordered list of review patches and a guarded migration manifest.
Supply the final successor digest instead if parent fixes land before rollout.
Re-run immediately before staging; stale resourceVersion tests should fail.
After staging, `--restore-from /operator-private/new-review.json --restore-target api`
generates only the API restore proposal; after its reader barrier, repeat with
`--restore-target maintenance`. Both options must be supplied together. Each
phase retains the inventory check and rejects changed UIDs, configuration,
resource settings, rollout strategy or partially updated images for its selected
target. The later maintenance phase does not try to re-restore the already
unpaused API. Do not reuse an earlier resourceVersion. An HPA replica change on
the selected API intentionally stops that API proposal for operator inspection;
the helper never overrides the HPA.

## Tests and remaining acceptance

Focused pytest tests cover exact patch scope; main/init pairing; immutable image
validation; all absent/false/true flag combinations; fresh resourceVersions;
UID/configuration/resource/strategy drift; unknown DB consumers/sidecars/mounts;
independent workshop/STT/HTTP workers; migration identity/resources; exact
37/38 ledger and prebuilt-index guards; and normal migrator delegation.

Live checks here are read-only. Applying migration 38, observing the actual
rolling update, recurrent historical metrics, public paths, active-job continuity
and final RDMA publication are parent-owned acceptance gates, not completed by
these unit tests.
