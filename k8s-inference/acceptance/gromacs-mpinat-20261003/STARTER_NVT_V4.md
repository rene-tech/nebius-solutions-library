# Alanine starter v4: analysis-only draft delta

The corrected selection passes CPU replay on the retained hosted NVT energy
file. The original selection reproduces the defect: native exit **0**, a missing
Density diagnostic, and only four exported observables. This is not a simulation
failure or a new hosted v4 qualification. No GPU run, bucket upload, runtime
change, default-image change or modification of released v3 occurred.

## Source and minimal change

Authoritative generator: the clean starter-data worktree at commit
`92b02648ab3390981561615da4194eb2e37be820`, `starter-data/md_examples.py`.
It inherits these energy commands from the canonical source requests. The main
backend's older generator was not overwritten or used to recreate v3.

Qualified v3 manifest SHA256:
`c8afd07ca1b6734c690839ba6d3eb4b461b65380852bcf705a863e7bd07a7709`.
The builder verifies all 497 listed objects before and after preparing the
draft delta. All 17 MD archives are retained byte-for-byte, with their inner
file hashes inventoried. No MDP, seed, topology, coordinate, checkpoint, physical
duration, timestep, integrator, thermostat/barostat, cutoff, PME setting, native
MD command, or non-GROMACS recipe changes.

Only `Density\n` is removed from the `energies-nvt` stdin field in:

- `alanine-quickstart`: one job;
- `alanine-1ns`: one job;
- `alanine-replicas`: two jobs.

The three `gromacs/parameters.json` files and their three embedded copies in
`recipes.json` are the six replacement objects. NPT/production Density selections
remain unchanged. The restart recipe has no NVT extraction; the umbrella recipe
already uses valid NVT fields and remains unchanged.

NVT density is explicitly `null`, with status
`unavailable_from_native_nvt_energy`; it is not zero, a fluctuating series, or an
unverified mass/volume derivation. Fixed-volume density could be derived in a
separate, input-bound analysis, but that is not necessary for this correction.

## Retained native replay

Original operation: `bc1bb480-4536-4147-aae1-bbb59504d841`.
Actual introductory protocol: **20 ps per phase**, not 1 ns or an equilibrated
ensemble. The source input archive SHA256 is
`8d2d7f61ddb7d329387fc64b2b36b27511f8575cfcaffdf2bc0763ed579f008e`.
Retained NVT EDR SHA256:
`3f391fd15117c3fc01d41586622723cee27c65ef0170f9cd8f2b1671be7dea56`.

Replay image:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:dc5d908c64503c4c4cdc3bede10987d49a9acdde5ef2f4f1d0e61f0628739f93`.
The helper checks the pinned OCI manifest and selected platform config against
the local immutable Docker image ID
`sha256:a4d91973c724ec3072e813d19a93d9d167455020a63f07c97b9fc7cc6da35275`.
This is the NVIDIA GROMACS 2026.2-dev single worker, not an MPI substitute.

Both analyses use CPU only, no container network, no GPU visibility, read-only
source mount, dropped capabilities, caller UID and explicit forwarded stdin.
There is no `mdrun`. Each observes 21 finite energy frames at 0–20 ps, 1 ps
cadence. The corrected output has exactly Potential, Total Energy, Temperature
and Pressure, with no selection diagnostic. Every numeric value in those four
series equals the originally retained output and the negative replay. Original
artifact hashes remain unchanged.

The validator requires exact requested observable identities and order, unique
contiguous legends, rectangular finite numeric rows, monotonic time, expected
frame count/cadence/extent, native exit success, and absence of missing or
ambiguous selection diagnostics. A successful exit or existing XVG alone is
insufficient. Sample means in the receipt are means of the 21 XVG frames, **not**
the higher-frequency native statistical averages or convergence estimates.
[Official `gmx energy` documentation](https://manual.gromacs.org/2026.2/onlinehelp/gmx-energy.html),
accessed 2026-10-03, distinguishes selected components and these averaging paths.

## Evidence and reproduction

Private evidence root:
`/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/starter-nvt-v4/`.

| Evidence | SHA256 |
| --- | --- |
| `delta-01/delta.json` | `372656e60acacf59fa16af4897835c6417111bd1efa43ae0c4eef9c7f0f15297` |
| `schema-validation-01.json` | `e8180068bdcc7bf5d357e911deaef06fbe47e0be57c975484c77a044ce0ed183` |
| `replay-04/receipt.json` | `240be88e855fb7fd83cf1f4be329a05a616d115462c249b54bf516d903c0dcd8` |

Replacements retain their original pack-relative paths under
`delta-01/replacements/`; every old and new object hash is in the delta manifest.
All three corrected parameter requests/four jobs pass the actual
`fs2_gromacs.contracts.normalize` validator, with its source hash retained.

From this acceptance directory:

```sh
python3 -m unittest -q test_starter_nvt_v4.py
python3 starter_nvt_v4.py build --base /path/to/verified-v3 --output /fresh/v4-delta
python3 starter_nvt_v4.py replay --native /path/to/retained-native-files \
  --source-receipt /path/to/lynx-agent-hosted-r4-native-verification.json \
  --local-image fs2-gromacs-starter-nvt-cpu:20261003-r1 --output /fresh/cpu-replay
```

The local image was obtained using existing registry authentication with
`skopeo copy docker://<exact-image-above> docker-daemon:fs2-gromacs-starter-nvt-cpu:20261003-r1`.
No credentials were minted or changed. The root-owned retained workspace's
three selected artifacts were copied to `native-copy-01`, then verified against
their original authenticated download inventory; the original tree was not edited.

Fifteen unit tests pass, including unavailable and ambiguous terms with exit 0,
wrong labels despite correct column count, duplicate legends, nonfinite/ragged
data, cadence errors, immutable source/delta behavior, fixed-volume checks,
and CPU command/stdin/UID isolation. Ruff and `git diff --check` pass.

Failed harness attempts are retained, not relabeled: `replay-01-failure.json`
records root-vs-user registry auth separation before execution; `replay-02`
records cap-dropped root UID unable to read the private input directory;
`replay-03` records omitted Docker stdin forwarding. The corrected caller-UID
and `-i` boundaries have a unit regression. These are CPU harness setup errors,
not native physics failures. `replay-04` retains the original v3 negative and
the corrected v4 positive separately.

## Integration boundary

This is deliberately a **draft v4 delta**, not a replacement full pack or a
qualified new data image. Root can apply its six replacements to a fresh copy
of the exact pinned v3 pack after checking each base hash, update the full pack's
version/documentation and identity metadata, add the explicit NVT observability
note, reset affected recipe qualification, and use the existing starter-data
qualification/seeding flow. Retained v3 evidence remains historical and must
not be relabeled v4. An affected hosted/client acceptance is still required
before promoting or seeding a v4 data image. Unchanged science does not itself
qualify a new customer-visible recipe or client path.
