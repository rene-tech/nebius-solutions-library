# September 25 admitted-pool recovery qualification

Bounded-path qualification is complete and deployed: two consecutive full native
recovery cohorts, cancellation, slow initialization and synthetic eligibility
return passed. All test hooks/resources and the disposable identity are retired.
`completed-matrix.json` in the private evidence root binds all positive receipts,
retained failures/corrections, exact releases and cleanup. This is not a claim of
whole-platform readiness or physical capacity certification.

## Exact deployed candidate

The operational candidate is source
`654ff215ea122a404e30fc0d64feacaa05102954`, tree
`acd8c57c4658cb1c2bc757c4d2e562bdd655e1a2`. Later commits add only acceptance
tools, tests and evidence; they do not change the deployed application.

| Component | Exact image digest |
| --- | --- |
| Gateway and model controller | `sha256:3840f45bf49e5736da009820f374da58897ca94b242fc62a3eb5a3f6121a78fb` |
| Preserved scientific CPU tools/companion | `sha256:719ec336ef93e582f3031735dba974b61ea97f1c4ce47e630fe18eae9e9cd77c` |
| Preserved GROMACS native runtime | `sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643` |
| Ordinary released customer CLI | `sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d` |

The application image repository is
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane`.
The source-archive build includes maximum provenance and an SBOM. The platform
manifest digest is
`sha256:8ad1c30c49653c422804b8c418a6b6f69f9ca8ed70b5cd56600f7a556adbf1da`;
the deployed index digest above was independently verified in the registry.

Helm release `fs2-serve-control-plane`, namespace `fs2-system`, revision **228**,
chart version `0.5.0`, was applied
`2026-09-25T06:03:47.115782Z`–`06:07:29.844960Z` to context
`fs2-remediation-sandbox2`, cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project
`project-e00rene`. Gateway 3/3 and model controller 2/2 became ready. Rollback is
revision **227**, previous application image `719ec336ef93…`. No schema migration
is required for this policy. Rollback removes automatic recovery, not persisted
attempt history. After the seed-only successor revisions, rollback must be
scoped against the current release so that it preserves the newer starter seed
image/configuration; historical revision 227 is no longer a whole-release
rollback target without sibling coordination.

The scoped release helper extracted the current live chart and caller values,
rendered the candidate, deep-compared every resource, server-dry-ran it and
rechecked the original revision before applying. Changes were limited to the
operational image, four recovery settings, read-only node/one-autoscaler-map
access and an explicit pin preserving scientific helper images. Starter seed
pins/tenants, model recipes/profiles/runtime digests, queue quotas, limits and
all unrelated resource fields were preserved. Existing customer LAMMPS Pods
retained their original UIDs and nodes while running across the rollout.

Private evidence root:
`/home/tux/secure-handoff/admitted-pool-recovery-20260925/`.
`build-654ff215e.json` binds the Git archive, OCI and attestations;
`rollout-01/` retains before/after snapshots and scoped render/apply receipts.
Private Helm values, tokens, TLS keys, client logs and native data are not
committed.

## Workload and declared gate

Public endpoint: `https://89.169.99.188/mcp`. Disposable ordinary tenant:
`admitted-pool-recovery-20260925`; key ID
`5bde31f2-31f4-4bd6-ab9e-e199acbc76bf`, owner
`3195b91e-45f3-549d-8da1-ed249fa177eb`. It has only ordinary GROMACS scopes and
the existing concurrency/budget/rate policy; no limit was raised.

The pinned released CLI uploads and submits two unchanged real 2 ns GROMACS
umbrella windows and downloads every returned native artifact. Source archive
SHA-256: `208f9167db1c499e98b169cdc920bbe6ab955a2299418960e116372fc5e322ab`.
Derived request SHA-256:
`1d55cf04442b6415d1f7e242627d1cf299dc6bfe0e2dfc0c55006daf82a7190d`.
Only the selected job list and output transport differ from the original batch;
the scientific job objects, seeds and protocols do not change.

The temporary CREATE-only injector adds the existing dead pool as an additional
required constraint for exactly this tenant/model/stage/shard's first attempt.
Kueue still owns real reservation and admission. The platform, not the runner,
records failure, deletes the immutable old attempt and creates the next one.
No Node, quota, Kueue status or other customer's Job is patched. Full injection
identity and configuration hashes are retained with every cohort.

The predeclared SLO is healthy-pool readmission within **300 seconds after
confirmed unavailability**, excluding destination initialization. With an
already-confirmed old dead pool, confirmation is admission plus the configured
120 seconds. Both public persisted failure timing and independent unscheduled
Pod/node/autoscaler snapshots must agree. Successful native output, idempotent
replay, non-overlapping attempts/reservations, unchanged release/injector,
accounting and no leaked resources are separate mandatory gates. Two primary
cohorts must be consecutive, independently submitted and use the exact same
release, key, fixture, client and injection plan.

## Retained negative evidence

- `cohort-preflight-failed-01.json`: the injector observer initially treated
  Kubernetes' omission of an empty NetworkPolicy egress list as drift. The
  canonical observer was corrected only for absent/empty deny-all Egress;
  nonempty rules still fail. No workload was submitted by that failed preflight.
- `cohort-01`, operation `f2405239-6d10-4a37-bfd4-318c443c3463`: the first
  task injector rejected the API server's `/mutate?timeout=2s` URL because it
  compared a literal path. Its deliberately fail-open policy admitted healthy
  work. This is **not recovery evidence**. Public cancellation completed and
  all its scientific resources were released. Real TLS query-path regression
  and an actual API-server dry-run proved the corrected injector before retry.
  Its exact old webhook/server namespace was removed with ownership checks.
- `cohort-02/receipt.json`: the completed customer operation was successful,
  but its offline observer raised `AttributeError` on the original attempt's
  pre-failure `recovery:null`. The original failed receipt remains unchanged.
  Correction `df6636f17` changes only that recovery verifier's null handling;
  the runtime observer in `6101abd0a` also checks actual `imageID`, not the
  runtime-resolved config digest in `ContainerStatus.image`. This distinction
  is permitted by the [Kubernetes container status contract](https://github.com/kubernetes/api/blob/v0.33.0/core/v1/types.go#L3000).
  Every retained started `imageID` matches the exact pinned native image.
  These are read-only observer corrections, not customer CLI/request/runtime
  changes. Full native validation, complete artifact checks, four accounting
  clocks, release/injector identity, idempotency and resource cleanup passed in
  separate revalidation at `06:35:39.758813Z`. No physics was rerun and no failed
  receipt was overwritten. The owner explicitly approved this treatment under
  the current customer release policy.
- `synthetic-01/synthetic-return-intent.json`: after a valid 130.955826-second
  stable unreserved interval, the first synthetic return removed the injected
  predicate from its suspended Job only. The pinned Kueue v0.17.8
  [PodSet equivalence implementation](https://github.com/kubernetes-sigs/kueue/blob/v0.17.8/pkg/util/equality/podset.go)
  ignores affinity-only changes, so its existing Workload retained the copied
  test predicate. That return was incomplete and did not demonstrate recovery.
  The owner explicitly approved removing only the second injected copy from
  the same unreserved Workload. Exact UID/resourceVersion/status/owner and
  otherwise identical native Pod-spec checks, then a server dry-run, passed.
  The separate `synthetic-workload-return-{intent,dry-run,applied}.json`
  receipts preserve the correction. No Job, Workload, reservation or status was
  deleted or recreated; no physical node/flavor/quota changed. Kueue then made
  the actual admission decision. This remains synthetic eligibility-return
  evidence, not physical fleet exhaustion or operator-assisted primary failover.

## Live outcomes

`cohort-02`, operation `26d64c0b-2341-4072-a68f-c4704780d447`:
window-01 admitted to `h100-1x` at `06:18:01Z`, failed automatically with
`infrastructure/admitted_pool_unavailable` at `06:20:02.212385Z` after
121.212385 seconds, and retained `retry_not_before=06:20:17.212385Z`.
The released old attempt was replaced on `h100-ondemand-1x` at `06:20:21Z`:
20 seconds after the confirmation boundary, 18.787615 seconds after detection.
The original healthy sibling completed without retry. Both native windows
finished all 1,000,000 steps / 2 ns, each with 2,000 nonzero trajectory frames,
finite geometry and verified native pull-coordinate correspondence. All **244
artifact entries** were downloaded and checksummed. They represent 200 unique
content-addressed artifact IDs; repeated identical files across windows are
legitimate and remain independently verified by entry.

The unscheduled failed attempt recorded **0** scheduler/device/active GPU
seconds, separately from 125.403431 quota-reserved seconds. All four accounting
clocks reconcile with the per-attempt lifecycle ledger. Actual billing is not
claimed. The revalidated receipt is separately retained at
`cohort-02/receipt-revalidated.json` and includes the original failure hash.

`cohort-03`, operation `da081e56-2e25-414d-9cdf-b2d6744188c3`, is the next
sequential primary cohort, started after the first revalidation. It admitted
window-01 to the dead pool at `06:36:31Z`, detected failure at
`06:38:31.322424Z`, and readmitted on healthy capacity at `06:38:48Z`:
**17 seconds after confirmation**, 16.677576 seconds after detection. Its
unchanged native completion and all final checks passed at `06:46:36.316489Z`.
Both primary cohorts passed under exact release/config digest
`4e75b4813b6bf33bfa98802bedec0d2333a808a53148c97e7c9cc1ad09dbe9fc`.
Their separate receipts are bound in `two-cohorts.json`; `timings.json` records
20-second and 17-second confirmation-to-readmission measurements against the
predeclared 300-second SLO. Four real 2 ns windows completed in total.

The shared deployment lock was handed back to the concurrent starter-data owner
only after both primary cohorts passed. Its seed-only successor revision **230**
was observed deployed at `06:58:19Z`, preserving this operational image and all
scientific runtime/tool pins. The separate initialization and synthetic
eligibility-return cases started after that explicit final handback. They bind
revision 230 separately and do not retroactively change the primary cohorts'
exact revision 228 evidence.

Cancellation operation `163ba414-b4f7-44a3-be45-955c8e65dec9` passed on the
same release after actual dead-pool admission, with idempotent replay and zero
remaining owned resources. It did not manually recover any workload.

`init-01`, operation `bbeeee80-e3ec-47fb-96cf-fd002f7bfcd1`, passed all final
gates at `07:12:44.334912Z` on revision 230. Its healthy replacement attempt
`82b8cd96-a2e1-5d7b-8051-1a8ce0f7a0c3`, Pod
`3a33e1c4-9b8e-4b0e-afc4-f7d4676ab10a`, ran the same-image initialization
pause from `07:02:05Z` to `07:04:35Z` (**150 seconds**, exit 0). It was not
evicted or retried again. The original healthy sibling and that replacement
both completed their unchanged 2 ns windows; complete native artifacts,
semantic/trajectory checks, accounting, idempotency and resource cleanup passed.
This is real initialization-progress protection, not physical scale-from-zero.

`synthetic-01`, operation `0c03dc73-4b42-4336-b794-b5ec1d9285b6`, passed all
final gates at `07:18:44.583977Z`. Its single retry remained unreserved with an
explicit Kueue affinity/no-fit reason for the measured 130.955826-second hold,
without another attempt or Job/Workload churn. The incomplete first predicate
removal and owner-approved second-copy correction are detailed above. After the
complete synthetic return, Kueue admitted the same Job
`c06ac05f-b998-48f7-a92a-2faf153c2451` / Workload
`606d5019-b2cc-4518-82e1-1c4c421b0e74` on healthy capacity at `07:10:39Z`.
Its unchanged 2 ns native window succeeded; the healthy sibling also succeeded
on its original attempt. All 244 artifact entries, native semantics/trajectory
integrity, four accounting clocks, idempotency and zero remaining owned
resources passed. No third attempt was created.

Both additional cases bind the same revision-230 release/configuration digest
`2eb1ed1b72c51ca854a7cce2e02b81f8f946bd61ac35a6a12a1ab58e5bfe557d`.
Their receipt SHA-256s are respectively
`b65f1928801cf9e8378bb6650353bd4ae71b2f6133b7f8d29a041be5c8e60481`
and `35cca4a3e89a0074852dbc0f00fb6c311bc9ea7b277d037b638a0d8641da5dc2`.
The separately retained predicate-copy application receipt is
`5048fb05db0558c2c6e0d91d7bb0ab2545110db37f5976afa37264a7c77e60be`.
Across the two primary and two additional cohorts, eight real 2 ns windows
completed; every native artifact entry was independently downloaded/checked.

## Local regression evidence

- Focused controller/real disposable PostgreSQL suite: **43 passed**. Covers
  durable restart budget/backoff, compare-and-swap races, cleanup barriers,
  exhaustion, missing/incompatible alternatives, cancellation, slow starts,
  scale-up/scale-from-zero guards and independent shard progress.
- Observer/admin regression set: **147 passed**, three existing database skips.
- Helm/chart/starter suite: **142 passed**, including preserved scientific-tools
  pin and exact new read-only RBAC boundaries.
- Acceptance, real TLS injector, controller reservation-envelope, release-diff
  SLO timing, runtime image-ID, disjoint-case, copied-predicate and revalidation
  tests: **160 passed**.
- Broad scientific sweep: **1034 passed**, 15 skipped, five unchanged Amber
  fleet-count fixture failures and 20 pre-existing child-delegation fixture
  registration errors. These were reproduced as baseline issues and were not
  patched as part of recovery. The new PostgreSQL fixture import is robust.
- Changed operational files pass Ruff. New modules pass mypy; the broader
  invocation retains an unchanged CLI graceful-shutdown float/int mismatch.
  The recipe refresh check retains the pre-existing unsupported
  `cosmos3-lerobot-augmentation` refresh path. All frozen recipe/runtime paths
  are byte-unchanged from the predeployment source.

## Supported scope and limitations

This is shared scientific-batch pre-start recovery, not cloud node repair or a
new scheduler. Fast recovery deliberately requires positive old-node and fresh
autoscaler evidence; uncertain states retain the original two-hour startup
window. Scheduled/init/running work is never failed by this pre-start policy.
Original attempt budgets, scientific compatibility, priority/lane/fair-share
policy and accounting remain; a new Kueue Workload receives a new creation time,
so original equal-priority FIFO position is not promised.

The additional cases passed cancellation during dead-pool admission,
150-second same-runtime initialization without eviction, and task-only
synthetic eligibility loss/return with 130.955826 seconds of observed
queued/no-churn state. Synthetic eligibility is explicitly not physical fleet exhaustion, and
the initialization pause is not a real cloud scale-from-zero event. Those two
physical conditions cannot be safely manufactured by taking customer capacity
or modifying node groups/quotas and will not be claimed as live-tested.
Native semantic/trajectory integrity does not establish PMF convergence.

The primary injector was removed at `06:48:24.632278Z`, webhook first, followed
by its verified server/TLS resources and exact disposable namespace. Native
artifacts were retained. Separate additional-case instances have disjoint
`-init` and `-synthetic` names/namespaces; actual server dry-runs verified both
first- and second-attempt mutations without creating GPU Jobs. They were removed
at `07:20:56.083708Z` and `07:21:48.952214Z`, respectively: exact webhook first,
then UID/owner-verified Deployment, Service, NetworkPolicy, ConfigMap, TLS Secret,
ServiceAccount and namespace. The initialization cleanup recorder encountered an
exclusive-filename collision after deleting its webhook/server; those absences
were reverified against the original UIDs and recorded before continuing with
unique step receipts. It did not cause scientific or shared-resource mutation.

The final absence audit checks all **six** task operations, including failed and
cancelled tests, and **16** recorded Job UIDs: zero Jobs, Pods or Kueue Workloads
remain. The disposable key was revoked and its owner disabled at
`07:21:31.874462Z`; a fresh ordinary `/v1/me` call returned HTTP 401. Artifact
objects and private receipts remain. The synthetic local PostgreSQL container
was already stopped/removed at `06:51:48Z`; no platform database was removed.
Generated ephemeral injector resources can be recreated from the retained private
bundles. `post-seed-release-230.json` confirms the unchanged exact release and
all three gateway plus two controller Pods ready, selected by actual Deployment/
ReplicaSet ownership rather than unrelated completed maintenance Jobs.

The unrelated failed-native diagnostic upload collision remains the separate
ready Task Deck follow-up `fs2-failed-native-diagnostic-upload-identity-r20260925`.
Its negative Amber evidence and required scientific helper/profile requalification
are preserved there. This recovery task does not claim that collector bug fixed.
