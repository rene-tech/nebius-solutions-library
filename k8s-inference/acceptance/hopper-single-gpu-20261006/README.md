# Single-GPU H100/H200 optimization — unchanged Lynx workload

Task: `fs2-hopper-single-gpu-lynx-performance-r20261006`.
Clean parent: `3e25ba2ca2c716453226a82f4cafce38ccf161f0`.

Reuse the L40S experiment lifecycle, native measurement/validation and independent
artifact analyzer. Each pod requests exactly one GPU; this is not multi-GPU
execution, even when one device belongs to an eight-GPU host. The running Lynx
L40S node is excluded. No customer API key, bucket mutation, shared deployment,
host power/clock policy, GPU mode or infrastructure change is in scope.

The original 185,486-atom CHARMM membrane TPR and production worker digest are
the same as the [L40S comparison](../l40s-incremental-20261006/README.md). Only
finite `nsteps` differs. Preserve 310 K, 2 fs, constraints, physical cutoffs,
PME tolerance, original output cadence and initial state. Neighbour-list tuning
retains the original positive Verlet buffer tolerance and native adjustment.

## Plan and boundaries

1. Separate H100 and H200 eight-CPU, one-GPU pods on currently idle nodes.
2. Three alternating-order 50,000-step screens for eight-thread control,
   four/six threads, CPU bonded placement, original PME grid, list 100/300,
   explicit physical-core affinity and AVX-512 when actually supported.
3. At most one combined candidate when at least two independent knobs improve
   the screen median by more than 2%. Confirm the best non-baseline candidate
   against baseline with three alternating-order 500,000-step pairs (1 ns each).
4. A separately labeled H100 full-host CPU-envelope experiment may reserve
   32 CPUs but still only one GPU, comparing 8/16/32 threads. This does not
   change the public eight-CPU allocation or qualify a new API resource shape.
5. If used, CUDA timeline capture is diagnostic only, in an isolated pod using
   the existing unprivileged scratch profiler overlay; no counter/host changes.
6. Validate all native checkpoints, finite energies, coordinates, atom count and
   requested trajectory files; independently rehash downloaded artifacts and
   remove only exact task-owned pods. Report native/process-inclusive rates,
   not unmeasured API-delivered gains. Compare hosts with hardware caveats.

## Reproduction

```bash
python3 -m pytest -q acceptance/hopper-single-gpu-20261006/test_hopper.py \
  acceptance/l40s-incremental-20261006/test_experiment.py
python3 acceptance/hopper-single-gpu-20261006/run_hopper.py \
  --gpu H100 --cpus 8 --mode tune --node <idle-node> \
  --name fs2-lynx-perf-cpu-hopper-h100-r1 \
  --tpr <private-approved-original.tpr> --output <new-private-evidence-path>
```

Run from `k8s-inference`. H200 uses `--gpu H200`. The optional expanded CPU
experiment uses `--mode cpu-envelope --cpus 32` on an idle full H100 host.
Profiling uses `--mode profile --profile-tools /opt/nvidia/nsight-systems/2025.6.3`.
No profiling wall time is mixed into throughput comparisons.

## Status

Preparing live measurements. Evidence root:
`/home/tux/secure-handoff/fs2-hopper-single-gpu-20261006/`.
No result or production improvement is claimed yet.
