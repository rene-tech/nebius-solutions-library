# SAI-20 PostgreSQL ingress and control-plane API egress

## Candidate boundary

This source-only candidate adds `fs2-control-db-ingress` in `fs2-data`. The
policy selects only CloudNativePG Pods labelled `cnpg.io/cluster=fs2-control-db`
and admits PostgreSQL clients from exact namespace-and-Pod label pairs:

- the control-plane runtime, model controller, maintenance, migration and
  bootstrap components in `fs2-system`;
- the separately reviewed website and customer-storage components, when those
  additive sources are integrated;
- Grafana and run-bound Terraform acceptance Jobs in `fs2-observability`, plus
  run-bound acceptance Jobs in `fs2-system`;
- CloudNativePG peers and the CloudNativePG operator for HA/lifecycle traffic;
- Prometheus on the metrics port only.

The control-plane chart receives a final values overlay whose
`networkPolicy.kubernetesApiCidrs` contains only the `default/kubernetes`
Service host route and ready API endpoint host routes. The target private
subnet CIDR is deliberately excluded from this effective runtime allowlist.
Scientific artifact destinations remain governed by their existing exact
`/32` or `/128` contract and are not broadened here.

CloudNativePG documents that the operator requires TCP 8000 and 5432, that
instances must communicate with each other, and that instance metrics use TCP
9187. Those preservation paths are explicit and do not grant application
access to a namespace as a whole:
<https://cloudnative-pg.io/docs/1.26/networking/> and
<https://cloudnative-pg.io/docs/1.26/security/>.

## Source provenance and integration

- Original task parent: `83bcb2d6c7f4dc112e414e00596e0d6b03e22712`.
- Reconciled tracked `origin/main` parent:
  `0e6fdf6d9f61e5737dc6ac5cec4c0111207dd697`.
- SAI-03 clean successor inspected for overlapping model-network ownership:
  `cea63190aca6548d8be961a9432cc7cc1277721e`.
- SAI-08 candidate inspected for additive website/customer-storage database
  clients: `6eb13e345c8b17420d1217a70d83e4974497b2b0`.

Neither sibling candidate is merged here. An integration owner must reconcile
their accepted descendants and rerun the complete policy contract before any
shared rollout.

The application-peer rules trust protected workload labels. They must not be
rolled out without the accepted SAI-03 admission/ownership boundary that
prevents an arbitrary workload creator from self-assigning those labels.

## Deferred verification and rollback

The coordinator boundary for this authoring pass forbids tests, formatters,
Terraform/Helm commands, cluster inspection and deployment. The regression
suite in `tests/test_sai20_network_isolation.py` is authored but intentionally
not executed. No resource, image, Helm revision, database, provider, registry,
credential or customer workload was inspected or changed.

After independent source acceptance and integration with the then-current
deployed source, the rollout owner must record the previous Helm revision and
run at least:

1. render/validate the workloads Terraform and control-plane chart;
2. confirm `kubectl -n fs2-data get networkpolicy fs2-control-db-ingress`;
3. prove an unlabelled scratch Pod cannot connect to
   `fs2-control-db-rw.fs2-data.svc:5432` within a bounded timeout;
4. prove runtime, migration, maintenance, bootstrap, Grafana, acceptance,
   CloudNativePG HA/status and Prometheus metrics paths remain healthy;
5. rerun customer inference, MCP, admin, operations/results/artifacts/uploads,
   storage, queue/model admission, observability and request-debug smoke tests.

Rollback is the previous reviewed workloads Terraform/Helm revision, or a
normal revert of the eventual integration commit. This candidate does not
authorize a rollout and is not a GO decision.

## Independent-review correction after `07faac62`

Independent review rejected commit
`07faac62c6854a7b7947f97f59b5b7b1030813fd` / tree
`6d56efe1edf36bb60cd272f86e585433673d0229`. Preserve that candidate as
negative evidence; its SAI-03 custody claim and SAI-08 compatibility statement
are not promotion evidence.

The exact SAI-08 source at
`6eb13e345c8b17420d1217a70d83e4974497b2b0` / tree
`bd55519891c3f653f11465bf59e997170ff8bf4a` labels the database-backed
customer-storage Pod `storage-reconciler-v3`, injects `FS2_DATABASE_URL`, and
allows egress to `fs2-data` on TCP 5432. The successor ingress rule now matches
that component only when both its storage egress and rollout generation labels
exist. The retained legacy `storage-reconciler` path is unchanged. The
cross-source fixture records the exact source paths and Git blobs so a review
cannot silently substitute another SAI-08 generation.

The cited SAI-03 source at
`cea63190aca6548d8be961a9432cc7cc1277721e` / tree
`3b16346454d84fe2cd3dad7dc468b850615371a5` is explicitly rejected as a custody
dependency: an object selector on top-level labels does not protect direct
Pods or labels nested in controller templates. The successor therefore adds an
API-server-native fail-closed policy with no object selector. It inspects the
effective Pod labels on Pods, ReplicationControllers, Deployments, StatefulSets,
DaemonSets, ReplicaSets, Jobs and CronJobs in both `fs2-system` and
`fs2-observability`. A controller identity is accepted only for a Pod carrying
a controller owner reference; controller templates require an exact release
writer identity.

The database NetworkPolicy now carries an explicit custody annotation. A
separate exact-name policy denies deletion or unauthorized mutation of that
NetworkPolicy, the two admission policies and bindings, and the namespace-local
writer Roles and RoleBindings. Existing broad RBAC cannot bypass this admission
deny. The workloads stage also requires a non-secret v2 handoff containing an
independently accepted source commit/tree, review receipt, RBAC census receipt,
impersonation-guard receipt, and exact writer/controller/custodian identities.
The rejected SAI-03 commit cannot satisfy that input.

This correction is still a source candidate, not proof that such a handoff has
been accepted. Under the coordinator boundary no test, render, validation,
plan, deployment, live inspection or negative connection probe was executed.
Integration and live status remain **NO-GO** until a distinct reviewer accepts
the exact successor and supplies the custody handoff.
