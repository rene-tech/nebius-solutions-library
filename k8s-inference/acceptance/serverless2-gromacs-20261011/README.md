# Scientific AI application / S2 GROMACS evidence

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

The S2 runtime source was rechecked at `5e97b75eb` on 11 October: the agreed
application-I/O/identity/attempt contract is still absent from its API. The
provisional wire adapter is therefore not enabled against that release. No
Serverless runtime or infrastructure files were changed by this application task.
