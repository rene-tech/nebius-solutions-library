# Concurrent customer experience trial

Completed 2026-09-07. Operator-assisted pilot only: this simulation exposed blockers to unattended customer use. No production fixes were made by this lane.

## Results and independent rating

[report.json](report.json) preserves every request, exact UTC/runtime identity, failure, phase, source hash and evidence digest. Traffic ran from 14:53:07.957Z through the last response at 15:22:33.112Z. There were **70 successful requests out of 71**, with no client retries.

| Surface | Passed / attempted | Median public latency | Empirical p95 |
| --- | ---: | ---: | ---: |
| Qwen HTTP | 36 / 36 | 2.018 s | 2.331 s |
| Qwen MCP | 34 / 35 | 3.148 s | 3.928 s |

The MCP population includes the failed request's 0.962-second failure boundary, not a successful response. Successful responses overall ranged from 1.282 to 4.142 seconds. Controlled phase labels were baseline 3/3, during 64/65 and recovery 3/3; phase transitions are not exact workload start/end boundaries, so use the retained timestamps for precise overlap. The scientific campaign terminated at 15:20:44.960Z and the recovery label began around 15:21:24Z.

Documented monitoring checks passed: `/readyz` 14/14, general discovery 18/18 and scientific discovery 18/18. The first four general-discovery checks used academic scope and correctly returned its one permitted serving model; the corrected 14 checks returned all 14 serving models. Scientific discovery returned all 10 profiles throughout. Five separate academic-token scientific MCP discovery/status/result calls passed; these were read-only and not scientific submissions.

My subjective experience ratings are **7/10 for interactive APIs, 4/10 for unattended scientific batch and 4/10 for the admin UI**. These are not statistical scores. The separate workload lane completed 13/14 scientific operations: its four-design RF batch recovered two preempted attempts automatically, but one impossible BindCraft placement required manual cancellation. The UI did not make that cause actionable. The platform is usable with an operator and API result retrieval; it is **not ready for an unattended or unassisted customer trial** until the placement, transient routability and result-access issues are corrected and retested.

## Scope and reproducibility

The existing public H100 platform is exercised by one short, synthetic Qwen request every 25 seconds, alternating HTTP and actual MCP. Three fixed prompts require `42`, `Katze`, and `ACGT`; each response is checked exactly. There are no client inference retries. Separate periodic checks cover documented `/readyz`, general discovery with the existing general key, and scientific discovery with the existing academic key. The scientific workload lane owns all scientific submissions and any bounded cancellation.

This lane also used a real Chrome browser with an existing bootstrap-administrator session. It evaluates operator-assisted usability, not a customer-scoped login, self-service onboarding, scientific validity, sustained throughput or a production availability SLA. Credentials stay in memory; no session storage or credentials are exported.

The latency clock spans the public non-streaming request through the complete validated response, including HTTP/MCP session setup, network, admission and status/result polling. It is not time to first token, GPU decode throughput or model cold start. Baseline and recovery samples are small; empirical percentiles are omitted for groups with fewer than 20 observations. Failed requests remain in the latency distribution and availability counts.

Scripts:

- [interactive_sampler.py](interactive_sampler.py): immutable per-request records, explicit phase and stop files, no inference retries.
- [browser_session.cjs](browser_session.cjs): read-only navigation and screenshots using an in-memory authenticated browser session.
- [scientific_mcp_readback.py](scientific_mcp_readback.py): five read-only scientific MCP calls against already completed RF/Protenix operations; no scientific submission or new GPU work.
- [summarize.py](summarize.py): redacted report, contiguous sample checks, original-source/raw-receipt hashes, exact monitoring-handoff gap, all failures preserved.
- [test_summarize.py](test_summarize.py): report semantics, failure retention, cadence and incomplete-run checks.

Private artifacts are retained in the existing H100 acceptance workspace, under `releases/trial-customer-20260907/experience/`. Public exports contain only synthetic outputs, operation/runtime identities, observation times and evidence hashes; raw browser DOM and screenshots remain private.

## Findings to carry into customer readiness

[browser-findings.json](browser-findings.json) records the exact pages, timestamps and evidence names. The main findings are:

1. BindCraft's second run was admitted to a one-GPU flavor that could not fit the complete Pod CPU request. It required explicit cancellation by the workload owner. The UI called the attempt running and showed no error, rather than an actionable unschedulable cause. This blocks an unattended customer batch; no new limit or capacity increase was attempted.
2. Successful RF and Protenix result pages have no wired artifact-download action even though the authorized API downloads work. Independently, immutable completed results are mislabeled stale because result commit time is treated as live observation freshness. Neither finding demonstrates artifact loss or an authorization failure.
3. The run summary does not expose reconciled allocated/active/idle/grace GPU-time totals. Some lower phase durations are present, but these cannot be substituted for trustworthy per-run accounting.
4. The scientific catalog calls qualified snapshot options unavailable while the full inventory correctly offers them. A normal-loaded *run* saying restore not observed is truthful; the defect is the model-level capability wording.
5. A partially admitted four-shard RF run exposes its individual attempts usefully, but the aggregate admission summary simultaneously says pending, shows an admission timestamp, and says no admission event was observed.
6. One Qwen MCP request failed server-side: the tool reported that the registry model was not routable even though the transport returned HTTP 200. The client retained only `ExceptionGroup`; the independent observer recovered the server error. Later requests were separate scheduled probes, not retries. The underlying route transition remains under diagnosis.

Positive observations include direct run-ledger/detail navigation, tenant and service-class attribution, stage/attempt progress, explicit retryable preemption, semantic validation, full-model inventory and correlated observability links. Five academic-token MCP discovery/status/result reads passed. These are not claims that every scientific profile was submitted over MCP or that every external dashboard query was tested.

## Preserved harness limitations

The first monitoring episode used nonexistent `/healthz` and an academic token for general-model discovery. Its 404s and scoped one-model response are retained separately from inference failures. A parent-approved graceful correction preserved the original request cadence and all original records; the exact start-to-start handoff was 25.021729 seconds. The corrected episode uses `/readyz` and the intended general/academic discovery keys.

One manually mistyped Protenix URL caused two frontend 404 responses; the exact operation URL worked. Initial unauthenticated session lookup 401 is the expected login exchange. The CLI browser VM could not read credentials; the repository's existing in-memory Node Playwright pattern was used instead. None of these harness mistakes is silently converted into platform downtime or a successful scientific request.

No production settings, policies, keys, hot floors, capacity, limits, drivers, images or deployments were changed by this lane. The final browser view showed 24 configured models, Qwen hot 1/1, Cosmos intentionally cold 0/0 and scientific profiles batch-ready; this does not erase the actual BindCraft fit failure under load.

The sampler exited successfully at 15:22:54.645826Z after three successful recovery requests. The browser closed at 15:22:54.832Z; its Node process exited successfully after stdin EOF. Neither task-owned helper remained. All 70 returned Qwen operations were terminal-success before their PASS records; the failed MCP tool returned no operation ID. This lane created no scientific Jobs or other cluster resources and retained the existing Qwen serving Pod. The parent owns aggregate resource-cleanup verification and the final commit.

Local verification: eight focused summarizer tests passed. The Task Deck skill supplied the evidence/handoff structure; the browser skill prompted the documented CLI-first attempt and in-memory fallback, and GPU-performance guidance kept public request latency distinct from GPU startup/throughput claims.
