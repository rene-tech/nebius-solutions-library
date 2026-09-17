# External DaemonSet admission fence

This is a separate security-owner root. It is not part of the workloads state,
does not use the workloads or release kubeconfig, and may only consume an
externally signed append-only generation map. Each generation creates one
fail-closed `ValidatingWebhookConfiguration` that sends DaemonSet
CREATE/UPDATE/DELETE and Pod CREATE AdmissionReviews to the externally hosted
`daemonset_fence_server.py` source bundle.

The service descriptor-reads a root-owned, read-only signed runtime registry
for every request. That registry adopts existing DaemonSets by live UID and
canonical spec without object annotations or mutation. It also binds the
authenticated maintainer and DaemonSet-controller identities, exact successor
spec, readiness evidence, and each transition phase. A local durable SQLite
transaction makes retries idempotent and prevents one signed transition token
from authorizing different request bytes. Readiness and completion advance
only through a new signed ledger generation; admission annotations cannot
assert either state.

The same external endpoint also serves a signed, short-lived reconciler
activation epoch and a cutover admission webhook. Reconciler Deployments are
created at zero replicas. Scaling a predecessor down or successor up requires
the exact content-bound transition, external-owner identity, immutable
Deployment UID/spec, and the appropriate quiescence/readiness phase. Pod CREATE
is admitted only for the active rollout generation and authenticated native
controller chain.

The separately hashed `storage_reconciler_cutover_executor.py` is the only
workload-shift mechanism and runs continuously inside the external cutover
service. It uses resourceVersion/UID/spec CAS to scale, never
deletes or replaces a workload, observes every predecessor Pod terminal before
successor activation, and emits a content-bound observation for the next
externally signed epoch. During the intermediate `QUIESCED` epoch every
reconciler rejects cloud work. Each new process also holds one global
PostgreSQL advisory lease for a reconciliation pass, so process overlap cannot
produce two concurrent inventory writers. Rollback is a higher epoch and is
accepted only after successor quiescence plus zero-inflight, schema-compatibility,
and provider-continuity receipts.

Generations are additive. Every installed webhook remains fail-closed and
reads the same signed protocol. `prevent_destroy` and the separately anchored
retained-generation receipt make omission, rollback, or state laundering a
hard failure. This task does not plan or apply this root.
