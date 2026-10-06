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

Live measurements started at approximately 09:53 UTC. Evidence root:
`/home/tux/secure-handoff/fs2-hopper-single-gpu-20261006/`.
No result or production improvement is claimed yet.

## Runtime and evidence contract

Pinned worker:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:ca863f44c7d17c8096267ec43939cda8b9546f0d3b11440e62bcf9149bc94a1e`.
Original TPR SHA-256:
`e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.

Each native execution retains its full command, allocated resources, input
identity, GPU samples, CPU consumption/throttling, wall duration, completed
checkpoint step, native ns/day, energies, coordinates and original-cadence
trajectory. The two finite TPRs differ from the approved original only in
`nsteps`; differences and native topology comparisons are retained. Performance
repetitions start from the same input state: these are not independent ensemble
samples or a claim that the scientific system has converged.

`native_ns_per_day` is GROMACS' own performance line.
`process_inclusive_ns_per_day` is completed simulation ns divided by the entire
native process wall time, including initialization and native checkpoint/I/O.
Neither is API acceptance-to-durable-object throughput. Do not add the short
screen gains together, extrapolate the instrumented trace as a benchmark, or
call a faster native configuration a deployed customer-default improvement.

The profiler is an isolated scratch overlay of Nsight Systems 2025.6.3. Its
five-second CUDA timeline is collected 45–50 seconds into a separate 0.5 ns
run; the full finite run must complete and validate before copying artifacts.
No host counters, privileged execution or production telemetry changes.

## Primary references used to choose candidates

- [GROMACS 2026.2 performance guide](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html):
  benchmark CPU/GPU task placement, thread count, SIMD and neighbour searching
  for the actual workload. GPU-resident mode can still include CPU force work;
  native wait counters are not isolated PCIe-transfer measurements.
- [GROMACS 2026.2 environment controls](https://manual.gromacs.org/2026.2/user-guide/environment-variables.html):
  CUDA Graphs remain conditional on run support. The earlier unchanged-input
  L40S experiment did not establish graph execution, so this task does not
  advertise it as an available speedup or alter CHARMM forces to enable it.

Source examined 2026-10-06. This is execution tuning, not a change to the
scientific protocol or a comparison against a different public benchmark.

## Completed H100 diagnostic (not a throughput benchmark)

`h100-profile-r1` completed a validated 250,000-step run and five-second CUDA
capture. Its owned pod was removed and the customer pod remained unchanged.
Independent download verification checked 57 files / 93,204,316 bytes.

- GPU-event span 4.987293 s; union of GPU kernel/copy/memset activity 4.383286 s
  (87.889%); overlapping streams are counted only once in this union.
- The largest kernel was short-range Ewald/Lennard-Jones force-switch:
  5,996 calls / 3.257320 s summed duration. PME, bonded, update and constraint
  GPU paths were also active; this is not wholesale CPU fallback.
- H2D 14,689,505,688 bytes / 0.525793 s of copy activity;
  D2H 14,293,156,080 bytes / 0.298973 s.
- Host CUDA event synchronization 3.269235 s. This includes waiting for GPU
  dependencies and is not a separately removable transfer-only cost.

Inference: simply adding CPU threads or HBM capacity is unlikely to transform
this workload. GPU force computation and CPU/GPU synchronization both matter.
The trace does not quantify attainable speedup or isolate every transfer's
cause. Comparing the previous L40S trace is diagnostic only: neither trace is
an uninstrumented performance trial, and hosts/drivers differ.
