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

## Required retest

- Fix and deploy through the new fleet's Terraform, retaining the old/shared
  fleets. Record the exact source commit and mirrored image digest.
- Submit to non-default south without a redundant `region` and verify the
  actual worker. Verify actual H100/H200/L40S/RTX PRO 6000 device names and CUDA
  kernel results, not just acceptance, queue labels or an exit code.
- Verify terminal operation records and API-accessible logs. Test cancellation
  after worker admission on the exact release with custom cluster IDs.
- Restrict worker API access using the observed operator addresses and control
  NAT pool, one worker at a time, then rerun connectivity/job checks.
- Multi-node/InfiniBand performance, GROMACS, legacy REST/MCP compatibility,
  customer-storage grants, preemption/resume and migration remain separate
  qualification gates. Do not report them as passed from these CUDA checks.
