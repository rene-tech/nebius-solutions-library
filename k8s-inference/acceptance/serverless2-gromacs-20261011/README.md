# Scientific AI application / S2 GROMACS evidence

## Integrated pilot — in progress

API 0.11.11 / jobs 0.1.14 includes the agreed S2 changes. The three application
contract probes pass against S2 source `56aaa9eb3`. The application unit suite is
now **86 passing tests**. API/worker images are being published for the isolated
`s2pr2` pilot. **No live GPU verdict yet.** The earlier CPU-only evidence below is
retained as history, not substituted for the live tests.

`pilot.py` is the reproducible operator driver: `prepare`, `register`, `submit`,
`status`, `replay`, `cancel`, `resume`. It reads only the existing internal
`sciai` identity and demo storage. Private configuration/state/receipts live in
`/home/tux/secure-handoff/scientific-ai-s2-pilot-20261011` (0700, outside Git).
The isolated Terraform root is `components/scientific-ai/deploy`; it creates only
the pilot namespace, configuration Secret, API Deployment, Service and path route.
Use its private local state, never the customer platform's Terraform state.

Live gates (pending until evidence is recorded):

- New synthetic GPU work submitted through REST and MCP, replayed with the same
  key, completed and retrieved with every artifact hash and native step checked.
- Real RTX and L40S placements select their matching digest-pinned native image.
- A committed mid-run checkpoint survives abrupt loss, crosses clusters without
  the old PVC, and resumes with `-cpi`; preparation executes exactly once.
- Unauthorized access and a terminated/replaced attempt cannot obtain new object
  URLs; previously signed URLs have an explicit 60-second residual lifetime.
- A failed result transfer cannot be reported as success or discard its recovery
  data. No broad customer-data or PHI release is implied by these pilot checks.

The fixture is a **new 4096-particle LJ argon** simulation with seed 1741,
2-fs timestep, NVE and a 120-K initial velocity distribution. It is a runtime
recovery test, not a biomedical accuracy or production-performance benchmark.
Every run retains its request, interface, acceptance time, polled S2 state,
native result, manifest and downloaded content-addressed output files.

## Earlier local CPU gate

11 October 2026. Scope: application contracts, SDK/transport behavior, and a
fresh real GROMACS **CPU** checkpoint/recovery execution. **Not a fleet,
cross-GPU, public-customer-path or HIPAA acceptance verdict.**

Completed checks for this source:

- 57 application/SDK/offline contract tests passed on Python 3.13 and again on 3.11.
- 157 existing GROMACS runtime/MPI contract tests passed using the existing
  control-plane test environment, with imports pointed at this candidate source.
- 1 opt-in real-engine native recovery test passed in 23.39 seconds: interruption
  at step 185,320, then completion at 600,000; preparation executed once.
- Ruff, whitespace checks, wheel build and clean-environment wheel imports passed.

The full legacy test suite initially failed collection in the deliberately lean
application environment because it imports the old control plane and other MD
adapters. It passed after using the existing control-plane test environment with
all MD module paths; no old control-plane dependencies were added to the new package.

The runnable fixture and checks are in
[`components/scientific-ai/tests`](../../components/scientific-ai/tests).
The engine image is pinned in `native-acceptance.json`; it is the existing
2026.4 RTX/H100 build executed **without a GPU** for this local test. It does not
qualify that image on L40S (its CUDA build has SM90/SM120, not native SM89).

Test failures retained in this report:

- The initial native fixture completed 150,000 steps before its first six-second
  segment boundary and the evidence assertion misread a command's argv field.
  It was not counted as interruption coverage. The fixture now runs 600,000 steps
  and asserts a strictly mid-run checkpoint and `-cpi` on resumed MD commands.
- No scientific engine or protocol changes were made to hide that test gap.

Passed scopes: owner/job/image restrictions, upload denial/corruption, manifest
integrity, stale epoch rejection, unchanged-file reuse, transfer-only recovery,
private input redaction, capability refusal, two credential renewals followed
by denial, actual boto3 S3 request construction, and unchanged legacy defaults.
SDK stubs and local lifecycle/storage doubles are used for these boundaries.

The native test uses synthetic argon, seed 1741, the real GROMACS binary, a fresh
TPR and native checkpoints/trajectory files. It moves the original workspace out
of the expected path before restoring. All raw files remain in the retained test
directory named in the machine-readable evidence; no customer data is included.

Pending: real S2 binding/lifecycle adapter, cloud provider policy enforcement and
revocation, optimized RTX/L40S images, native GPU performance, cross-cluster loss,
public REST/MCP, protected scratch/cleanup and the full unchanged-release customer
acceptance cohorts. These are gates, not silently passing skips.

At the earlier source `5e97b75eb`, the agreed execution contract was absent. That
historical blocker is superseded by the integrated pilot update above. No
Serverless runtime or infrastructure source files were changed by this
application task.
