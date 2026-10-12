# New-fleet qualification — 12 October 2026

This records infrastructure checks, **not scientific-model or customer release
qualification**. Only the new `scientific-ai2` fleet and its `system/qa` identity
are test targets. Existing customer endpoints, jobs, keys and storage are not
modified.

## Provisioning

- Source initially `49bf3bf1b`; per-cluster Service CIDRs added by the Serverless
  owner in `99ab1dcfb`, documentation corrected in `184e24f80`.
- Initial plan: 59 creates, no updates/deletes. South and supporting services
  created; two north cluster creates failed because no default `/16` Service
  block could be allocated in the reused VPC.
- North/control now autoallocate `/20` Service blocks. Repair: 11 creates, no
  updates/deletes. All three clusters are RUNNING, each with two Ready CPU nodes.
- Six GPU pools exist with zero minimum nodes, spot follow-price and reservation
  policy `FORBID`. The full H100 pool has its own InfiniBand GPU cluster.
- The operator's Kubernetes API source was verified from audit logs, not an
  external IP service: north `195.242.18.145`, south `213.239.176.243`. The control
  allow-list correction was one in-place update, followed by three successful
  node queries.
- Worker Terraform applies: 118 creates each; control: 127 creates. All completed
  without manual in-cluster patches. Both MultiKueue workers connected. API and
  console have two ready replicas, PostgreSQL initialization/key bootstrap jobs
  succeeded. Console and health URLs return 200 over validated public TLS;
  unauthenticated model listing returns 401.
- Existing public routing check at 18:08 UTC passed every website, admin, API and
  MCP check on `https://89.169.99.188`. The Forge hostname remains untouched.

## Live API findings — API 0.11.24

The successful Terraform applies did **not** establish working GPU execution.
The first real API requests exposed these gaps, passed to the Serverless owner
for a source fix and regression tests before migration:

1. `allowed_clusters: [south]` without a redundant top-level `region` was rejected
   with `region north is not in allowed_clusters ['south']`. An implicitly
   selected default region was incorrectly treated as an explicit constraint.
2. Generic `container-run` requests with `[h200]` and `[l40s]` GPU preferences were
   admitted to `system-prefer-h100`. Their annotations retained the requested
   preferences, but the rendered queue and pool affinity did not enforce them.
   Worker pods selected `h100-spot-1x`, triggering H100 scale-out for all three
   requests. A completed kernel alone would not prove correct GPU placement.
3. Admitted-job cancellation returned `region north is not served by this API
   (available: ['control', 'eu-north1', 'eu-south1'])`. The client resolver only
   special-cased `hub`; arbitrary configured cluster IDs `north`/`south` were
   not mapped to their region-keyed kubeconfigs. This also affects pod/status,
   log and attempt-record verification paths.

### Failed test closeout

R1 north submissions `op-e053de844edf6183`, `op-96ccb3e4387980a3` and
`op-a4c6a34543f28301` were cancelled successfully over the QA API before admission
after the south request failed. They are not GPU passes.

R2 submissions `op-e5db979450763db0`, `op-563d447fd22991ec` and
`op-ab03d0b8598bd1c2` demonstrated the wrong-GPU placement and alias cancellation
defects. API cancellation was attempted and failed with the error above. At
18:25 UTC, only those three exact QA manager Jobs were deleted from the new
control cluster; MultiKueue removed their worker pods. No customer Job, volume
or bucket was deleted. The test process exited nonzero and retained its receipt.
Ephemeral Job objects are gone; request/operation evidence is retained in the
private receipts and this report. The autoscaler owns any resulting idle nodes;
no GPU floor was added to compensate.

Private receipts (mode-0700 parent):
`/home/tux/secure-handoff/scientific-ai2-20261012.KvdP28/`.
`gpu-verification-r1.json` and `gpu-verification-r2-north.json` are failed runs.
They must never be cited as four-GPU-family qualification.

## Source repair and isolated rollout — API 0.11.26

The Serverless owner fixed all three defects in `f356222ab` and `3462425f3`,
with regression coverage (`make check`: 216 API tests plus console tests,
reported by the source owner). Final source is
`3462425f3937a29d1375575e1063d9ca941f0123`; fleet chart is 0.1.5.
API 0.11.26 was mirrored into the new registry with identical digest
`sha256:0e25bd7945a201d2e6a00c3ad22064f7abd8a8e5efdbb0804bdbd3a5ddb23661`.

Both worker plans/applies changed only their fleet chart. The following control
apply updated its fleet chart, API and billing workload and replaced its two
internal key-bootstrap Jobs. All completed cleanly; both API replicas report
the expected image digest. No old/shared fleet was updated.

R3, submitted at 18:33 UTC, accepts south without a redundant region and renders
the correct GPU-specific queue, pool affinity and actual worker pod selector
for all four families. H100 `op-e289219cdf10f1ce` succeeded with 65,536 validated
CUDA results, actual `NVIDIA H100 80GB HBM3` identity and API-accessible logs.
H200 `op-a517912895ae9904` and L40S `op-ba8906274dd6ad3a` also passed with
matching device names, 65,536 validated outputs and API logs. Submission to
actual main-container start was 52 s for H100 on an already available node,
309 s for H200 from zero and 307 s for L40S from zero. These are single
infrastructure observations, not model cold-start benchmarks or capacity bounds.

North cancellation `op-7be3d97d4abf12cb` was requested only after RUNNING and
a successful CUDA kernel appeared in API logs. The API acknowledged CANCELLED
at 18:36:50 UTC. The configured 120 s grace elapsed; no worker pods remained
at 18:39 UTC, and the API still reported CANCELLED, ended 18:38:48, without
a recovery successor. Cancellation acknowledgement and GPU release are distinct.
After retaining the receipt, the exact QA-only 2 GiB work PVC was deleted;
its scratch files are not retained, while operation/log evidence remains.
The dispatcher also removed the three earlier orphan QA claims. Only the
platform's shared scratch/weights claims remained in the north test namespace.

Both worker Kubernetes API allow-lists are now restricted to the audit-observed
operator and control NAT sources. Each was changed independently through
Terraform, then checked by three operator queries and MultiKueue connectivity.
System admin `/v1/fleet`, internal key listing (two active keys) and the Grafana
admin login using Terraform credentials passed. The existing customer public
routing suite passed again at 18:38 UTC.

### Additional fresh-fleet finding: RTX platform driver profile

South node `computeinstance-e07nr1fgrqak19524p` registered Ready with instance
type `gpu-rtx6000-a`, but the bundled Nebius GPU Operator chart's driver profiles
only included `gpu-rtx6000`. No driver DaemonSet matched the actual node, so
driver validation waited and no GPU became allocatable. This is a deployment
configuration defect, not spot capacity exhaustion. The source owner fixed the
chart overlay in `082de8f2a`, refined in `709bb41b8`: retain all existing profiles
and add a matching profile for each configured GPU platform missing from them.
Both new workers received a single GPU Operator Helm update through Terraform.
South's matching driver was created without recreating or relabeling its node.
Final source pin: `709bb41b8b914d5963a51cef1c1abb90d94d6a8c`. At 18:47 UTC,
all four final cloud/platform plans returned **zero changes**, exit code 0.
At 18:49:07 UTC the **four-family R3 verification passed**. The original south
request `op-47da578bbc45e8cd` completed on `NVIDIA RTX PRO 6000 Blackwell Server
Edition`, with 65,536 validated CUDA outputs. All four completed operations
have an attempt ID, epoch 1, an immutable runtime digest and a final result
reference, with kernel evidence retrieved through the API. The RTX elapsed time
includes time waiting for the driver-profile fix, so is **not** a valid clean
cold-start benchmark. Its node was neither recreated nor relabeled.

North's GPU workers subsequently autoscaled back to **zero** without operator
intervention (18:51 UTC: only its two system nodes remained). GPU instance checks in both projects confirm spot
follow-price with reservation policy `FORBID`. The final customer-cluster read
still showed 27 Ready nodes and 38 Ready GPUs. No customer mutation occurred.

The provider also warns that managed PostgreSQL's bootstrap password is stored
in protected Terraform state. Adoption of the newer write-only provider field
is a security follow-up, not completed by this infrastructure qualification.

South cancellation `op-9b0ca15b3ad042fe` was acknowledged at 18:50:45 UTC after
RUNNING and a successful RTX kernel retrieved through API logs. After its
120 s grace, zero worker pods remained. The API reported CANCELLED, ended
18:52:44, without a recovery successor. Its exact 2 GiB QA scratch PVC was
deleted after retaining the receipt; raw scratch files are not retained.
**Both regional admitted-cancellation checks pass.**

Final public checks at 18:52 UTC: new API health, console and Grafana health
return 200 over verified TLS; all old customer public-routing checks pass.
The fleet stays deployed. No test workloads remain running; the south spot
node is left under its existing zero-minimum autoscaler, not manually pinned.

## Remaining qualification gates

- Multi-node/InfiniBand performance, GROMACS, legacy REST/MCP compatibility,
  customer-storage grants, preemption/resume and migration remain separate
  qualification gates. Do not report them as passed from these CUDA checks.
