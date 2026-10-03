# Continue the stopped MPI lane without replaying the PEP pair

`run_mpi_lane.py` is a narrowly scoped companion to the already-running
`run_matrix.py`, not a second admission-policy owner. Do not stop, signal, edit
receipts for, or restart the active parent. The borrower never logs into admin,
patches the QA key, calls `Campaign.execute`, or adopts/cancels the PEP jobs.

The initial matrix's MPI operation
`0a828a8f-a743-4055-b605-efe24dba9a1f` failed before native initialization because
artifact capability resolution compared the durable execution mode with the
literal shard name `gang`. Preserve that failed operation and its complete
original receipts as platform-failure evidence. This fresh run retries the same
input, not the old operation or its idempotency key.

After the owner confirms the exact fixed API image is fully rolled out:

```sh
k8s-inference/components/control-plane/.venv/bin/python \
  k8s-inference/acceptance/gromacs-mpinat-20261003/run_mpi_lane.py \
  --client-root /home/tux/worktrees/scientific-ai-workbench-lifecycle-20261002/templates/hcls-librechat \
  --qa-env /home/tux/secure-handoff/fs2-agent-reliability-20261001/reliability-r7/runtime.env \
  --fixture /home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/fixtures \
  --parent-output /home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/mpi-matrix-r1 \
  --parent-pid 749243 \
  --parent-plan-sha256 6383217be9d136106385cbd86a751ab60004384b3ea01e24f1a93a05dca01d64 \
  --failed-operation-id 0a828a8f-a743-4055-b605-efe24dba9a1f \
  --campaign-id fs2-mpinat-mpi-lane-r2 \
  --output /home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/mpi-lane-r2 \
  --cohort-order 2,3,5,6,7,9,4,8 \
  --expected-api-image REGISTRY/CONTROL_PLANE@sha256:EXACT_FIXED_IMAGE_DIGEST
```

Run from the backend repository root. The image is required, immutable and
checked against the Deployment and every owned live API Pod before preparation
and before each shape. Old, extra, terminating or unready readers fail closed.
The example image placeholder is deliberately not runnable until replaced with
the actual fixed digest; no helper run is evidence of deployment by itself.

The explicit order preserves every original case and command: REST 1x1, 1x2,
2x1; MCP 1x1, 1x2, 2x1; then REST and MCP 1x4. This lets the two-GPU shapes use
the available partial node while both PEPs remain on it. Four-GPU work may queue
until that node is free. There is no pool override, placement pin or customer
movement, and concurrent samples are not uncontended strong-scaling evidence.

The borrower freezes the parent plan hash, PID/start identity, original two PEP
IDs, prior failure, new campaign, expected API image and execution order. It
checks live QA operations before each submission, accepting only the original
PEP pair plus at most one own MPI operation. A separate local lane lock rejects
duplicate borrowers. If parent progress, identity or ownership drifts, new
admissions stop; closeout drains only the borrower's MPI IDs. Its inherited
capacity gate is read-only and records events in the **new** output directory.

The parent remains the sole writer of the reviewed QA limit 3 and eventual
restoration to 2. If it finishes while this MPI lane is still working, both PEPs
must be terminal and its saved restoration must be exactly 2. The borrower then
continues one serial MPI operation under that baseline, acquiring the vacated
matrix lock. It does not claim that restoration waited for all MPI cases. If
the parent disappears without restoration evidence, or another matrix obtains
the lock, new submissions stop. The helper never repairs policy automatically.

Use the same new output/campaign only to recover its own already-recorded
operations; a changed image, order or identity requires another fresh campaign.
A terminal failure stops the lane and remains a failure, not an automatic retry.
The total continuation deadline is six hours or the smaller explicit `--timeout`.
Retain `mpi-lane-plan.json`, `mpi-lane-progress.json`, ownership/image-check logs,
native timing/output receipts, and the original matrix in the final ledger.
