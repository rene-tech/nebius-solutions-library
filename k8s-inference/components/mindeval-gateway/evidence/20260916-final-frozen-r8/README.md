# Interrupted r8 candidate — diagnostic only, NOT frozen-release acceptance

The directory name reflects the original intent. The coordinator interrupted
the freeze after separate Magpie native-WAV response and workshop takeover-UI
defects were found. This cohort is retained as diagnostic evidence, not either
of the final required unchanged-release rehearsals.

Started 2026-09-16T17:50:51.724105Z against the public customer endpoint
`https://89.169.99.188`, with ten ordinary PATs for ten principals in the same
`mindeval-rehearsal` tenant. Protected credentials never entered these artifacts.
The existing self-signed rehearsal certificate was explicitly accepted.

Before submission, read-only Kubernetes/Helm checks confirmed:

- Workshop image `sha256:a7c5c981ea203c93a0439f305f654ae660d3a79c86aa8baa30b364b19ea07c93`
  (r8), Deployment generation 6, updated/available 2/2; Helm revision 6 deployed.
- Gateway `sha256:4d7c32408cdc7a8bb5df55b73408146d6fe5aee759927f9211790b0874ef2db7`,
  generation 1, one updated/available replica.
- CP `sha256:8f6e62ec3eaf4444683cf332cff3e4a7d39eccb1db99cfb166df538709aadd5c`,
  generation 248, updated/available 3/3; Helm revision 137 deployed.
- Cluster context `fs2-storage-h100`, namespace `fs2-system`. No deployment
  mutation was performed by this test worker.

## Completed results

`collected-reports.json` preserves every customer report and gateway event log:

- 60/60 jobs completed with strict finite scores for all five named criteria,
  fixed Gemma27B judge, benchmark eligibility and exact five-message canonical
  transcripts. Six clinicians per team, two rounds, one profile per team.
- MindGuard completed all 180/180 patient-prefix observations without errors
  or truncation. These are model observations, not human-labeled accuracy.
- 300 successful inference calls, 1,594,058 total tokens, zero retries.
  Mean queue wait 15,951.975 ms and mean total call latency 21,902.459 ms.
- All 60 judge calls succeeded; 1,229,428 judge tokens, mean judge queue
  76,763.353 ms. Bursts were queued rather than converted to failed scores.
- Original sampling observed 50 running plus ten queued, never more than five
  running per team. All teams made progress. Sampling ended at 192.931 seconds
  (48 completed, twelve running); do not infer exact cohort completion latency
  from the later receipt collection.
- Ordinary-auth denial, 20-profile acceptance/21-profile rejection, idempotent
  creates, cross-team detail/events/report isolation, and separate controlled
  pause/takeover/typed-turn/nudge/resume/abort/reconnect checks passed.

## Why the original summary says `passed: false`

At 17:54:22 UTC only the local observer process was suspended to prevent its
automatic second-cohort submission while the coordinator prepared fixes.
The durable jobs continued. At 17:55:09 the observer was cancelled, retaining
its original summary and samples. No failed jobs were resubmitted and no second
repetition or full ten-round cohort was created under this label.

`scripts/collect_existing_rehearsal.py` then used GET requests only, recovered all
60 terminal reports, and applied the same strict scoring/classifier checks.
Collection ran 17:55:48–17:56:27 UTC. `collection-summary.json` records successful
existing-run validation but explicitly sets `release_acceptance: false`.
Two tests verify this collector never submits work, leaves the original summary
unchanged, preserves failed classifier results, and cannot promote interrupted
evidence to release acceptance.

The coordinator's browser/spoken checks may have added low-rate concurrent
work after this cohort's protected submission window ended at 17:51:26; these
are not a controlled isolated-throughput benchmark. No physical-node loss was
tested. Graceful speech Pod draining is separate evidence owned by that worker.
