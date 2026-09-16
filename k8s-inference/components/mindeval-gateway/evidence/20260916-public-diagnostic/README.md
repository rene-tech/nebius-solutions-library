# Initial public diagnostic — not final release acceptance

Executed 2026-09-16 against `https://89.169.99.188` with ten ordinary platform
PATs belonging to ten principals in tenant `mindeval-rehearsal`, max concurrency
five. Credentials remained in the protected handoff file and are not in evidence.
TLS verification was explicitly disabled for the existing self-signed rehearsal
certificate. No administrator credential was used on the customer path.

Gateway image `sha256:4d7c32408cdc7a8bb5df55b73408146d6fe5aee759927f9211790b0874ef2db7`
and workshop image `sha256:7cf93a79b10c1be956300ef81e41ebd55fc6ed3ff77e26ca209e68b3b545b65a`
were independently verified in the live Deployment specs: one gateway replica,
two workshop replicas, context `fs2-storage-h100`, namespace `fs2-system`.

## Results

- 60 durable jobs: ten teams, one profile/team, six public clinician models,
  two canonical rounds, Qwen30B patient and fixed Gemma27B judge.
- All 60 completed with exact five-axis finite scores. 300 successful inference
  calls, zero retries and no failed/aborted benchmark jobs. 1,531,790 prompt plus
  72,106 completion tokens (1,603,896 total).
- First queue progress for every team was observed between 12.141 and 19.088s.
  Peak observed concurrency was 50, exactly five/team with ten additional jobs
  queued. No sampled team exceeded five. All jobs terminal at 303.320s; fetching
  all reports completed at 345.157s.
- Mean per-call queue wait 18,123.052ms; mean total call latency 28,534.858ms.
- Missing/denied PAT checks returned 401/403. Cross-team detail/events/report
  returned 404. Repeating each create key returned the same six IDs and did not
  add hidden jobs to owner listings. A 20-profile gateway registration succeeded;
  21 profiles were rejected by gateway and workshop with 422, without extra
  inference jobs.
- Separate control run passed pause, reconnect, takeover, typed human turn,
  nudge, resume and abort. Late replies did not mutate the aborted transcript;
  the intervened run was excluded from benchmark eligibility.

## Actual integration failure

**All 60 workshop-internal MindGuard observations were unavailable.** Each
retained `gateway_http_error` and `enforcement: observe`; none invented a safe
classification. Replaying the first completed transcript via the public
`/v1/mindguard/assess` with its ordinary owner PAT returned 200/completed and
evaluated all three patient prefixes. Source inspection identifies the likely
internal/public authority mismatch: CP TrustedEdgeMiddleware requires the
public Host for `/v1/*`, whereas the workshop addressed the internal Service.
The coordinator owns the caller fix and fresh acceptance rehearsals.

The original diagnostic runner's `summary.json` says `passed: true` because that
version required explicit classifier coverage reporting, not availability. That
flag is **not** an end-to-end acceptance claim. Raw diagnostic evidence is
retained unchanged. Subsequent runner versions require completed, untruncated
assessments for every patient prefix and fail unavailable classification.

`repetition-1-reports.json` contains all customer-visible reports and gateway
event logs. No report was repaired or failed job retried. `controls.json` is the
separate intervention audit. `catalog.json` preserves exact public model
metadata and calibration provenance.

The sibling `../20260916-full-dialogue-diagnostic/full-dialogue.json` is a real
ten-round canonical Qwen30B dialogue (21 messages, eleven patient prefixes),
completed and strictly judged in 236.408s. Its built-in observer had the same
unavailable failure. It was handed to the MindGuard worker for independent
full-prefix evaluation; that worker owns the classifier benchmark evidence.
