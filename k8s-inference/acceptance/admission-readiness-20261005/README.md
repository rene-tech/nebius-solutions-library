# Large scientific restart admission: event-loop responsiveness

## Scope and status

This is a narrow follow-up to the simultaneous 20,007-file REST/MCP GROMACS
continuation qualification on October 5. It changes neither readiness deadlines
nor API-key limits, database pool sizes, ownership, resource requests or model
execution. Source qualification is not evidence that the live incident is closed:
the parent must deploy the exact successor and repeat the concurrent customer
paths with the existing readiness/database observers.

The task is `fs2-scientific-admission-event-loop-readiness-r20261005` in the
NIM Fast Start Platform Task Deck. No customer key or additional inference was
used for these source tests. Existing Lynx and internal simulation runs were left
untouched.

## Retained failure evidence

Live image `191e2c2be4c131c78fd190d620f71dc2b6cfff285c21ab1af4acfb452684c195`:

| Public path | Operation | Initial / replay admission | Failed readiness observation |
| --- | --- | --- | --- |
| REST | `7fb06cae-0eb6-44ab-8f5d-772d2fb3ae86` | 33.882 / 30.778 s | 18:35:36.867 and 18:35:39.066 UTC |
| MCP | `c2d128a9-80f5-418f-82a6-0372b4fdb7a7` | 25.576 / 21.975 s | 18:37:33.843 UTC |

The REST reader was `fs2-serve-control-plane-665d846f9b-bkk6x`; request
`d5d1116d-9914-4f11-b9af-5a8070f5cc68`. The prearmed sampler recorded
`database_unavailable` with the unchanged readiness deadline. Contemporaneous
database samples showed about 478–494 mCPU and no active application query.
That narrows the investigation but does **not** by itself distinguish event-loop
starvation from every possible pooled-connection problem.

Private evidence, retained rather than copied into Git:

- `/home/tux/secure-handoff/fs2-longrun-restarts-20261005/concurrent-20k-r1/readiness-reasons/`
- The same directory's `api-readiness-after-rest-admission.json` and
  `reader-snapshots/` captures.
- `/home/tux/secure-handoff/fs2-lynx-longrun-20261005/outbox38-database-load.jsonl`

## Measured finding and bounded fix

`PostgresArtifactRepository.list_artifacts` fetched scoped rows asynchronously,
then synchronously constructed and fully validated every `ArtifactRecord` on the
API event loop. A restart listed the whole workflow twice: once for checkpoint
selection and once for continuation input construction.

The fix:

1. Keep the same query, filters, order and full content-address/access validation.
   Decode the detached inventory with the existing two-worker
   `run_scientific_cpu` helper, **after** the database fetch releases its connection.
   Repeated cancellation drains an active decode before the task exits.
2. Reuse the exact inventory within one freshly authorized resume request.
   No cross-request inventory or authorization cache is added. Operation/status
   ownership checks and fresh input-artifact reads plus current token-policy
   checks at ordinary submission are unchanged. A replay performs a fresh scoped
   inventory read. The public checkpoint-discovery response does not expose it.

Small bounded artifact-read batches retain their existing behavior. No timeout,
pool-size or global database setting changes are included.

Local diagnostic measurements (Python 3.13.13, not a production throughput claim):

| Probe | Inline | Existing CPU helper |
| --- | ---: | ---: |
| Two 20,007-record decodes, unconstrained: worst heartbeat delay | 202.7 ms | 31.2 ms |
| Two concurrent 20,007-record decodes, two-CPU affinity + one CPU contender: worst heartbeat delay | 680.2 ms | 80.0 ms |
| Same contended case: total two-inventory decode time | 685.2 ms | 695.3 ms |

This fixes responsiveness, not the fundamental CPU cost. The contended probe is
an affinity-based local test, not a replica of Kubernetes CPU throttling.
`probe_artifact_decode.py` reproduces it using generated metadata, no network or
credentials, and terminates only its own bounded CPU contender:

```sh
cd k8s-inference/components/control-plane
PYTHONPATH=src .venv/bin/python ../../acceptance/admission-readiness-20261005/probe_artifact_decode.py --contended-two-cpus
```

Other candidates were measured rather than assumed: on an actual retained 20k
state, `ScientificBatchState.admit` took about 4 ms, and invocation rebinding
about 6 ms without profiling. They were not changed. Full state decoding was
expensive but already ran off-loop in the current release.

## Regression coverage

- Deterministic 20,007-record heartbeat/thread-boundary test; exact scoped query
  arguments and ordered record equivalence.
- Repeated cancellation while decoding: no abandoned worker or partial result.
- Invalid storage-key metadata remains rejected.
- Single-GPU and MPI continuation/replay retain original physical inputs, native
  checkpoint, lineage, new budget and idempotency, with one fresh list per request.
- Existing bounded input reads still validate every pointer/access value.
- Existing readiness failure/recovery, CPU cancellation, atomic admission and
  outbox owner tests remain in the qualification suite.

Use the checked-out GROMACS package during testing, not an older installed wheel:

```sh
PYTHONPATH=src:../../models/molecular-dynamics/gromacs/runtime .venv/bin/pytest -q \
  tests/test_scientific_artifact_list_cpu.py tests/test_gromacs_resume.py \
  tests/test_scientific_artifact_reads.py tests/test_readiness_timeouts.py \
  tests/test_scientific_cpu.py tests/test_scientific_outbox_recovery.py \
  tests/test_scientific_admission_cpu.py
```

Initial non-PostgreSQL qualification: **45 passed**, 11 database-dependent skips,
2 PostgreSQL-marked deselections. Separate PostgreSQL qualification: **28 passed**,
including the 20,000-output stage-commit test and the atomic admission/outbox
tests, against only the task-owned local `fs2_readiness_tests` database, never
the serving database. Ruff passed for all six source/test/probe files it checks.
The first red tests confirmed inline decoding blocked the event loop; an old
installed GROMACS wheel also surfaced an unrelated seven-day limit, resolved in
the test invocation by selecting the actual checked-out runtime, not by weakening
the fourteen-day assertion.

## Required live closure

Parent-coordinated exact-image rollout, followed by concurrent public REST/MCP
late-state resumes and idempotent replays. Retain per-reader readiness failures,
admission timing, original customer pod identity/restarts, existing queue/metrics
health, and terminal artifact verification. If failures remain, report them as
open; these local results do not prove all admission latency or readiness failures
are eliminated.
