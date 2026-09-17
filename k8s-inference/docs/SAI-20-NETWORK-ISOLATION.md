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
