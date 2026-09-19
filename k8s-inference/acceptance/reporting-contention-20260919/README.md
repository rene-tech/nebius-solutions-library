# Reporting contention: in-flight metrics reads and history diagnostics

This is a source repair over integrated backend `295b57486609636b9f1283010adca8267f79f050`.
It preserves that release's visual models, RF input documentation and accounting.
The source proof and later bounded release-189 observation are separate below;
neither establishes platform-wide customer readiness.

## Preserved failure and diagnosis

Release 188 returned 503 twice for the same retained scientific-run detail on
2026-09-19. Handler durations were 2.831 s and 4.023 s. Both observations remain
failed. The required history adapter has an unchanged two-second budget;
optional node-fit enrichment runs later and cannot produce this error.

The same gateway recorded 28 metrics requests during 15:02:20–15:04:40 UTC,
with median duration 6.898 s and maximum 13.161 s, plus a readiness failure.
The exact installed history adapter, mounted catalog and existing run succeeded
in 244 ms in a separate read-only process/pool. Its history was small: 19 events,
72 lifecycle signals, 11 correlation rows and two accounting rows. This does
not recreate the production pool wait or establish which original subread failed.

Protected evidence is under
`/home/tux/secure-handoff/scientific-unattended-20260919/fit-live-r188/`.
The diagnosis receipt `bounded-diagnosis.json` has SHA256
`d0f2758aab0681da1444bcf4aa472bc730bc534f52d37358969553276389fa8d`.
The separate continuation preserved ledger equality and verified the existing
sample count, general CPU/GPU placement and RF/Cellpose/scVI discovery. It did
not turn the failed original gate into a pass or verify the unavailable AF3 fit.

## Narrow changes

- Identical overlapping `/metrics` reads in one application process share a
  single in-flight task and identical response bytes. A later non-overlapping
  scrape always performs fresh reads. There is no completed-result/stale cache.
- All accounting, queue, transport, customer-operation and lifecycle reads and
  series remain present. Values are applied only after every read succeeds;
  failures are returned rather than falling back to old metrics.
- One cancelled caller leaves other waiters intact. Losing all waiters cancels
  the underlying read; application shutdown drains task cancellation before
  closing the store. Failed collections cannot poison a subsequent fresh read.
- A scientific history-detail request has one server-generated request ID in
  its response/problem, access log and `scientific_history_read` diagnostic.
  Diagnostics include operation UUID, elapsed milliseconds, fixed phase names
  and exception **class**, never bodies, SQL, credential values or exception text.
- Mandatory record, lifecycle and accounting reads split pool acquisition,
  query and release timings. App-inventory/catalog and event-repository timings
  are aggregate phases (their internal acquisitions are not separately traced).
  State decoding and detail projection have separate timings. Timeout
  cancellation identifies the phase while preserving the original 503 contract.

No pool size, resource limit, query bound, two-second adapter timeout or admission
policy changes. No global query rewrite without measured equivalence proof.

## Tests and production gate

From `k8s-inference/components/control-plane`:

```sh
.venv/bin/pytest -q tests/test_reporting_reads.py tests/test_scientific_admin.py tests/test_scientific_admin_fit.py tests/test_scientific_admin_postgres.py tests/test_scientific_admin_accounting.py tests/test_api_mcp.py -m 'not postgres'
```

Focused tests cover overlapping HTTP scrapes sharing a constrained simulated pool
with successful admin history; fresh counters; late read failure without partial
publication; shared errors; individual/all caller cancellation; shutdown; exact
adapter phase logging; timeout pool waits; request-ID correlation; and payload
absence. Existing API/accounting tests remain required. These are local service
tests, not a claim of production load qualification or an additional GPU run.
The scoped run passed **92 tests**, with three PostgreSQL-marked tests deselected;
Ruff and `git diff --check` passed. Protected JUnit evidence is
`fit-live-r188/reporting-repair-tests-r2.xml`, SHA256
`2897a79c3f3f484af860c7e1cb83a33e44fd668e5b29bdebd9e6ff696cf3a35a`.
The earlier test receipt remains intact: its lone failure was a test expectation
of HTTP500 where the existing RuntimeError mapping correctly returns HTTP503.
The final assertion requires the unchanged exact503 behavior.

The release owner must deploy an immutable successor, then perform bounded
concurrent metrics/history GETs and repeat the unchanged ledger, node-fit and
discovery assertions. Preserve every 503. If a failure recurs, use the matching
request ID and phase/error-class log to identify the actual bottleneck before
another change. Do not increase timeout/pool limits to mask it.

## Deployed release 189 — 2026-09-19 15:35 UTC

The release owner deployed source
`bbcec4d515d7d53cabc10d185d48adb4ddb6435e`, including repair `a18541`,
with immutable control-plane image
`sha256:2e72e6d90e76acdd051e6148a0bb562720cedb8e57744e60d88402a75df71a19`.
The admin image remained
`sha256:72581f9f4035742e8c0b52a197c9b9a702f17fe8a6a837990dd2f7bf42ce4ae4`.
Exact image/Ready verification covered three gateways, two controllers and two
admin Pods. No model runtime, pool size, timeout or resource limit changed.

One separately labelled app-bound group passed: six concurrent HTTP `/metrics`
reads inside the exact gateway Pod returned 200 in 3.524–3.528 seconds. Two
external public admin history reads overlapped them and returned 200 in 0.685
and 0.744 seconds. Reference-data history ran on the metrics-loaded Pod;
general history ran on another exact-image gateway. Matching server-generated
request IDs joined both diagnostic records: mandatory adapter time was 299.610
and 271.130 milliseconds, with direct measured pool waits at most 8.899 ms.
Identical metrics response hashes are consistent with shared collection, not an
independent SQL query-count proof. Coalescing internals have the separate tests
above. Existing Prometheus scrapes also remained active.

The same-release read-only gate separately confirmed unchanged ledger subject,
signals, correlations, event digest/watermark, phase/scheduler/device accounting
and all 12 retained sampled-activity values. General CPU, reference-data CPU and
both GPU-stage placement projections were present, as were RF input provenance
guidance and typed Cellpose/scVI discovery. Node-fit is an allocatable upper-bound
check, not a claim of free capacity or guaranteed admission.

Every failed harness attempt remains separate: six incorrect public `/metrics`
requests returned 404 because the public route is not exposed; six Kubernetes
Podproxy calls timed out at 30 seconds without response bodies; an initial local
launcher omitted stdin forwarding and failed before issuing any HTTP requests.
No route, policy or limits were changed to obtain the app-bound observation.
These failures do not replace the original release-188 503 evidence, and the
later successful group does not retroactively pass any failed attempt.

Protected root:
`/home/tux/secure-handoff/scientific-unattended-20260919/fit-live-r189/`.
The app-bound receipt `local-metrics-r2/receipt.json` has SHA256
`0d1cf3a6562d86a9c828fef9e86f51e26d5e16c16e12a35ffa83eec52fd4fdcc`.
`FINAL_SUMMARY.md` has SHA256
`a510cce37facb263b79907e63d755a72b6d814e7825359b12b173e45ed2e9936`.
The original aggregate receipt remains failed for its public metrics probes;
its independent ledger/fit/discovery checks remain individually passing.

Verdict: the repaired reporting path passed this bounded exact-release operator
regression, with no inference or configuration writes. Proxy reachability,
broader sustained-load behavior and whole customer workflows are not qualified
by this observation.
