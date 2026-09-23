# Native qualification evidence

This is a native single-GPU qualification, not customer-path release approval.
The exact candidate worker is
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/lammps-worker@sha256:e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c`.
Its engine is NVIDIA stable_22Jul2025 amd64:
`nvcr.io/nvidia/lammps@sha256:d8a0076dfe84fcbc98db05531993c1cd9deb964050b9c122b92655dc3685d731`.
The wrapper source is `54b90d9ca`; later qualification edits do not change that
runtime. Raw evidence root: `/home/tux/fs2-lammps-evidence-20260923`.

## Scope and reproducibility

`receipts/native-h100-first-v3.json` declares exactly six completed H100 cases,
one repetition each. Its `passed` status does not complete the entire two-pool,
three-repeat campaign. Each receipt binds input, request, result, every native
file, validation, worker image, Pod UID, GPU and driver. Earlier failed screens,
original validator errors and artifact-copy errors remain separate evidence.

The repeated campaign fixes physics, seeds, atom counts and output cadence:

| Case | Atoms | Production steps | Timestep / ensemble | Native context |
| --- | ---: | ---: | --- | --- |
| LJ | 131,072 | 300,000 | 0.005 reduced / NVE | FCC, density 0.8442, cutoff 2.5 |
| EAM | 32,000 | 150,000 | 0.005 ps / NVE | Cu, `Cu_u3.eam` |
| Tersoff | 32,000 | 300,000 | 0.001 ps / NVE | Si, `Si.tersoff` |
| SNAP | 2,000 | 150,000 | 0.001 ps / NVE | Ta, SNAP plus ZBL |
| ReaxFF | 8,000 | 20,000 | 0.1 fs / NVE | Carbon, CHO potential, QEq each step |
| Rhodopsin | 32,000 | 50,000 | 2 fs / NPT | CHARMM, SHAKE, PPPM 1e-4 |

All have 2,000 warmup steps and production trajectory cadence 10,000 steps.
Native `timer timeout` closes approximately 60-second segments. A complete
continuation script redeclares each force field, neighbor setting, fix, compute,
variable and output; independently decoded native restart steps must advance.

LJ uses reduced units: ns/day is intentionally null. Its stock-style benchmark
has unchecked neighbors every 20 steps and approximately 0.8% energy span.
EAM's stock-style delay-5 neighbor setting reports dangerous builds. These are
fixed performance baselines, not recommended accuracy defaults. Separate
`accuracy_control.py` runs retain identical physics with checked delay-0,
every-step neighbor decisions. The packaged LJ starter already uses those
conservative decisions. Neither the broad 2% NVE energy-span gate nor finite
trajectories establish material-specific accuracy or scientific convergence.

The separate H100 accuracy control is now recorded in
`receipts/native-h100-neighbor-control-v3.json`: only neighbor checking changes
to `every 1 delay 0 check yes`, with identical physics and production length.
LJ relative total-energy span drops from the three-repeat baseline median
0.00787585 to 0.00009703 (about 81-fold smaller); measured throughput is
0.7551 times baseline median. EAM span changes from 0.00017588 to 0.00016930
and throughput is 0.9800 times baseline median. Both have zero dangerous
builds. There is one control run per case, so no repeated-performance
significance or timestep/neighbor convergence is claimed. Baselines are intact.

ReaxFF is a constant-volume carbon numerical benchmark. Its post-warmup
temperature is around 1,200 K and computed pressure around 2.65 million atm;
it is not equilibrated 300 K carbon or a validated material prediction. No
thermostat, density or potential was silently changed to improve performance.

`validate_case.py` streams every atom in each trajectory, checks exact cadence
coverage against decoded native intervals, finite thermodynamics, requested
production length, final steps and restart boundaries. A segment shorter than
the dump cadence may have an empty trajectory part only if its decoded interval
contains no required frame. Original false ReaxFF empty-part failures are
retained beside corrected validations of the unchanged output. Numerical
restart continuity is measured only where pre/post thermo samples have the
same timestep; timer boundaries without a last sample are explicitly unmeasured.
The dedicated fresh-Pod recovery uses a fixed first boundary to close this gap.

## Native package and GPU inventory

The pinned package has LAMMPS 22 Jul 2025, Kokkos 4.6.2 CUDA plus Serial,
double precision, CPU FFTW3, Kokkos GPU KISS FFT and OpenMPI 4.1.5rc4.
It includes ASPHERE, CLASS2, DIPOLE, DPD-BASIC, DPD-SMOOTH, EXTRA-MOLECULE,
KOKKOS, KSPACE, MANYBODY, MC, MISC, ML-SNAP, MOLECULE, OPENMP, OPT,
REAXFF, REPLICA and RIGID. All required Kokkos pair styles are inventoried.
There is no separate GPU package; package names alone do not prove execution.

The H100 uses native SM90 cubins. The L40S uses compatible SM86 cubins and
emits Kokkos's explicit architecture-performance warning; it is not presented
as a native-SM89 optimized package. Inventory includes binary hashes, native
help, CUDA cubin/PTX architecture listings and actual GPU/driver identity.

Kokkos PPPM's KISS warning is not CPU fallback. Exact-source dispatch and
binary CUDA kernel symbols show GPU KISS. Same-source KISS/cuFFT builds and
SM86/SM89 builds are separate controls, not promoted runtime replacements.
No improvement is claimed until matched repeated native timings and Kspace
measurements exist. A changed control image requires its own acceptance.

LAMMPS source is GPLv2 and pinned to upstream commit
`c7ae612a9497437412cb787b78769570f48653dd`; the NGC recipe is retained as
`inventory/NGC-Dockerfile`. Control builds retain the source archive, license,
patch, compiler/package versions and CMake cache in `/opt/fs2/fft-build/`.
NVIDIA container components retain their own terms; a regional mirror does
not imply unrestricted external redistribution or customer entitlement.

## Timing boundaries and comparison limits

Native `Loop time` excludes initialization, restart decoding, checkpoint file
hashing and artifact copy. Receipts separately report worker wall time, native
command wall time, CPU user/system time and RSS, GPU samples, file volume,
native Output timing and host input/output-copy duration. The monitor is
included in child CPU accounting. `kubectl cp` is not a measurement of hosted
tenant Object Storage throughput. A recovered copy with lost host timestamps
reports null rather than an invented duration.

NVIDIA's public performance page uses different GPU counts and, for its
current table, stable_22Jul2025_update1. Its aggregate results are context,
not a matched speedup comparison with these one-GPU, eight-vCPU jobs. Inputs,
rank topology, output cadence and MPS status would all need matching first.
These tests do not enable MPS, MIG or driver changes.

## Deferred image scan findings

Trivy 0.70's exact-v3 scan reports 11 HIGH package findings across two CVEs,
zero CRITICAL findings. Full scan and SPDX SBOM are retained under `inventory`.
This is a point-in-time scanner result, not a claim of absence of vulnerabilities.

| Finding | Affected installed packages | Installed | Scanner fixed version |
| --- | --- | --- | --- |
| CVE-2025-68973 | dirmngr, gnupg, gnupg-utils, gpg, gpg-agent, gpgconf, gpgsm, gpgv, keyboxd | 2.4.4-2ubuntu17.3 | 2.4.4-2ubuntu17.4 |
| CVE-2026-45447 | libssl3t64, openssl | 3.0.13-0ubuntu3.5 | 3.0.13-0ubuntu3.11 |

At the parent's direction, patching is deferred to a separate image lane so
the exact scientific candidate remains unchanged. This is not a security waiver.
Workers remain non-root with all capabilities dropped, RuntimeDefault seccomp,
no service-account token and no storage credentials. Native LAMMPS syntax is
powerful; isolation, not a claim that input scripts are harmless, is the boundary.

## Customer bundle and later gates

`customer_bundle.py` produces one sustained six-case archive and six-job request.
The generated archive is at `customer-six-case-v3/input.tar.gz` (SHA256
`2b4adc1464162d2d6c042031b7bd1687578ce08a42dcaed5c435d8441f49df98`).
For downloaded artifacts, reuse `validate_case.py WORKSPACE --job-directory JOB
--output RECEIPT`, with root `result.json` and unchanged `data/JOB` paths.

Fresh-Pod native recovery, real hosted REST/MCP and tenant-bucket paths,
independent GPU process snapshots, and release acceptance remain distinct gates.
Neither a native restart nor a same-process pause is a GPU snapshot.

H100 native fresh-Pod recovery has now passed for LJ NVE and rhodopsin
NPT/SHAKE/PPPM, with distinct Pod UIDs, complete workspace transfer and measured
same-step restart continuity. Exact receipts are retained in this directory.
The first hosted six-case operation `f963df4d-1141-49c6-906e-1b05a2953b7a`
failed at customer checkpoint export with `BucketMaxSizeExceeded` HTTP400,
not native EAM. Its logs remain under the private handoff
`fs2-md-engines-20260923/lammps-hosted-six-case-01/`. No scientific input or
runtime change is justified by that storage failure; hosted remediation and
retry are parent-owned. A later successful operation must not replace this record.

Sources: [LAMMPS restart contract](https://docs.lammps.org/read_restart.html),
[Kokkos acceleration](https://docs.lammps.org/Speed_kokkos.html),
[exact source](https://github.com/lammps/lammps/tree/c7ae612a9497437412cb787b78769570f48653dd),
[NVIDIA benchmark context](https://developer.nvidia.com/hpc-application-performance).
