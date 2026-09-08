# Unscheduled serving startup: observed gap and bounded repair

The r03 scientific campaign passed all 14 requests, but exposed a remaining Qwen burst-startup gap. Two natural burst Pods were admitted, remained unscheduled and were removed after the existing 30-second idle cooldown. All 67 ordinary requests succeeded on hot capacity and no route withdrawal was recorded. This is still a startup-qualification failure, not a fully clean platform verdict.

The deployed source during r03 was `bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`. No production setting, driver, model recipe, hot floor, startup deadline or resource limit changed during observation or these read-only historical checks. Root owns the repaired source release and its subsequent live acceptance.

## Two concrete mechanisms

1. An unscheduled Pod has no Ready condition yet, and kube-state-metrics therefore emits no `kube_pod_status_ready` series. The earlier `ready==0` join discarded that real Pending Pod. The correction starts with the exact owned, active-ReplicaSet Pending/Running Pod set and excludes explicit `Ready==1`, preserving UID, deletion, terminal, desired-count and deadline guards.
2. A positive desired-replica scrape can arrive after the latest15-second history-subquery step. Adding only the current edge covers that instant but can still lose it permanently with faster off-grid scrapes. The durable correction retains the same actual scrape timestamp on a one-second history grid, plus the current edge. It does not replace the timestamp with evaluation time, change scrape frequency or renew the deadline from equal samples, decreases or Pod restarts.

Pod `...-jvnh2`, UID `8d4cf55b-0dfb-4abd-a952-d799f583de6a`, was admitted08:15:44Z and deleted08:16:14Z. Pod `...-77bpq`, UID `f03c9689-2eb1-463d-a14d-70a1dfb2ae21`, was admitted08:16:34Z and deleted08:17:04Z. Both belonged to `qwen3-8b-b300-burst-h100-1x-6d6575886f`. Retained events show KEDA1→0 deactivation and ReplicaSet deletion, not a proven Kueue preemption. They never reached image loading or snapshot restoration.

## Historical verification, not a fabricated live pass

| UTC evaluation | Observed source data | Deployed query | Corrected durable query |
|---|---|---:|---:|
|08:15:58|Desired1, owned Pending Pod, Ready series absent; positive scrape08:15:50.613|0|1|
|08:16:48|Before next scrape: desired still0, second Pod series absent|0|0, correctly no invented demand|
|08:16:58|Second Pod and positive desired count visible, before08:17:04 deletion|0|1|
|07:54:24|Previously restored burst Ready and idle|0|0 for900s and7200s budgets|

The readiness-only correction returned 0 at 08:15:58 and 1 at 08:16:08 because the 15-second history grid had not yet captured the 08:15:50.613 edge. That failed intermediate result remains retained. Before the first observed positive telemetry, neither query can know that a Pod exists; the pre-scrape zero is explicitly preserved rather than forced into a passing result.

Candidate `208a2e2009bdf535e192caaef84080a4a55347e9` contained the readiness-only change. It passed the full backend suite (1,633 passed,4 optional skips,77 external deselections,231.23s) and exact-image builds, but **was not deployed** because historical/off-grid checks exposed the clock gap. Its private receipts remain under `releases/208a2e20`. The later candidate receives its own tests, image identities, Terraform deployment and live qualification; those cannot be borrowed from the partial candidate.

## Tests and measured query cost

- New absent-Ready tests failed against the prior source before being corrected. The independent off-grid review also demonstrated that a current-edge-only fix could lose the edge after55seconds.
- The final focused suite passed145 tests, including actual `promtool` execution of31 scenarios/41 expression assertions. Five- and ten-second off-grid scrape fixtures retain demand at55s,65s,3m and949s, then expire exactly at950s for an edge at50s with a900s budget. Existing zero-demand, foreign/retired/deleting/terminal Pod, repeated activation, mixed-readiness/full-desired-count and no-renewal controls remain.
- Ruff and whitespace checks passed. Two renderer string expectations were updated from the former15-second grid to the intentional one-second grid; historical spec-digest assertions and runtime manifests remain unchanged.
- Three actual read-only HTTP round trips through the installed Grafana/Prometheus proxy for the900s query took0.106226,0.100162 and0.099913seconds. The7200s maximum-budget query took0.106263,0.106928 and0.108655seconds. All returned1 for the historical Pending case. These include network/proxy overhead, are a small bounded observation, and are not a fleet-wide Prometheus-load benchmark.
- No live model request, capacity mutation, Kubernetes write or policy workaround was introduced by the historical query checks.

Private evidence under `trial-customer-remediation-20260907/r03/observer/`:

| Receipt | SHA256 |
|---|---|
|`natural-qwen-missing-readiness.json`|`7946ac9a0bdcda9e2d10f76323e812a6d4992612a40f712308545b00a806492e`|
|`durable-query-first-and-latency.json`|`eb6d7b933e5de9f95ae98570929fd219f9b9e5f2d924f9f050f8ab44e1c3d4a4`|
|`durable-query-second-prescrape.json`|`5a96e6f4d8b7c0ea2cb3e9a57f85e7a4f7dad00841cc0fa5d3e8c0b93138c51f`|
|`durable-query-second-postscrape.json`|`909cf528db440ede5e0241fe404a1c4da862ef0813a41da5485e9be465e02c70`|
|`durable-query-ready-idle.json`|`258953570484e8c0394cde26b3f092de1595145fd8ebdcd0f482e41d7ee8a630`|

Per-Pod/scaler/deployment event receipts and every intermediate failed query are retained alongside them. The read-only `capture_qwen_startup_metric.py` now accepts an explicit historical evaluation time and diagnostic expressions, records HTTP timing and keeps raw payloads private.
