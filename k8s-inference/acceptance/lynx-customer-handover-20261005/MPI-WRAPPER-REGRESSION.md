# Explicit nonempty selector: MPI wrapper compatibility

Measured on 2026-10-05. This is a **bounded native compatibility regression**,
not public API/MCP qualification, multi-node/RDMA performance evidence, or a
completed customer-continuation handover. The separate customer-file test
validates zero-byte XTC handling; this regression does not duplicate that test.

## Immutable release identity

- Wrapper source: `c5c66f320c56ef484b47ff3773d9082ef1e4449c`.
- Repository: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs`.
- Unchanged live engine base: `sha256:c8321a27df6490e3ca33d7f2d80ee5d5a4917b787c612e98c1704346f1cbdece`.
- New wrapper image: `sha256:c10a9b2edfab1678153ca45c05c00e4649979065ef2ecadfb69a17b83483f662`.
- Linux/amd64 platform manifest: `sha256:caae319b9fdde3d324fdbabc70b3100b49bec6650f66db7b6c332ab8eb01ed0b`.

Built from an exact `git archive` of the committed runtime with
`Containerfile.worker-release`, the immutable base above, BuildKit provenance
and SBOM enabled. Registry digest and source/base labels were checked. No
native GROMACS, CUDA, MPI or UCX rebuild was performed.

## Matched fixture and result

The existing MPI MPINAT benchmark fixture retained its complete input archive:
`a2748bf521f68334fa0ebb38a5cd5957a82b4a9718b948e1fc9ac253342743d2`.
Only the three `eneconv` file selectors gained explicit `nonempty: true`.
The request SHA-256 changed from
`c1efd2cf82e01a02de9b1e9b7caf59e9a9bdfc1957eff6b6cfff2c199d7d9247`
to `3c52cedc04e02dd490c8dd89e4e4d863bebf27949a73670cc19c15115017ec07`.
The control remains three 10,000-step / 20 ps native repeats, not an
ensemble-convergence or sustained-throughput study.

| Observation | Result |
| --- | --- |
| GPU | One NVIDIA H100 80 GB HBM3; driver 580.173.02 |
| Allocation | One GPU, eight CPUs, 16 GiB; no other workload moved |
| Native wall time | 99.813 s |
| Native execution | All ten commands exited zero; all three checkpoints reached 10,000 steps |
| Results | All three energy/temperature samples finite; all final structures contain 81,743 atoms and finite coordinates/cells |
| Artifacts | Complete inventory; 35 files independently rehashed |
| MPI binding | Complete rank bindings; actual device identity retained |
| Cleanup | Exact owned Pod deleted; absence observed at completion |

The ten **native GROMACS argument vectors** are identical to the previous
successful fixture, including all three expanded `eneconv` input lists.
The full `mpirun` launcher differs from the older reference only by the
previously introduced `-x FS2_GROMACS_MPI_TRANSPORT` environment export. This
known intervening transport change is recorded, not normalized away as full
launcher parity. The selector extension itself did not change simulation
settings or the selected nonempty energy files.

Task Pod `fs2-models/fs2-mpi-nonempty-20261005-r1` ran on existing node
`computeinstance-e00cjp2bfywsenpzzv` from 20:10:25 to 20:13:03 UTC, with actual
image identity matching the new immutable image. It used local native
checkpointing, **not GPU snapshot restore**. No customer operation, API key,
floor, capacity, public route, deployment or release catalog was changed by
this test.

## Retained evidence and next gate

Private evidence root:
`/home/tux/secure-handoff/fs2-lynx-longrun-20261005/release/mpi-nonempty-20261005`.

- `build-metadata.json`: exact source/base build arguments, provenance and OCI identity.
- `fixture/`: old/new request identity and unchanged input archive.
- `native-h100-mpi/receipt.json`: validation checks, actual image, times and cleanup.
- `native-h100-mpi/workspace/result.json`: actual commands and native results.
- `native-h100-mpi/workspace/`: retained native artifacts and checksum inventory.

Receipt SHA-256: `18d5b5836b8c8441bbfa1eacb010a1d5fb41a9509e2b4820a6fba07737babb88`.
Build metadata SHA-256: `1d7030c90874e0b7f02f1a3e45863f24c0eb875d1e02fc9d74530937b6c8cadc`.

The coordinated release must bind this worker alongside the new single-GPU
worker before publishing the additive selector schema. Customer readiness
still requires the demo-owned public import/continuation and six-hour soak
listed in [the handover](README.md); this native receipt does not waive them.
