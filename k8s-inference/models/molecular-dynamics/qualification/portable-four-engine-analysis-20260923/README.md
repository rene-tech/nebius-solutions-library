# Final-client portable four-engine analysis

The exact final client `81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d`
successfully regenerated the complete GROMACS, NAMD, AMBER and LAMMPS analyses,
four real-trajectory clips and synchronized 2×2 grid from the delivered bundle.
The [sanitized receipt](receipt.json) binds all exact identities and evidence.
This is CPU-only portability acceptance, not new simulation, browser acceptance
or a whole-platform readiness claim.

The container had four CPUs, 8 GiB, `--network none`, no GPU or API credentials,
a read-only root filesystem, and only `/delivery` (read-only) and the fresh
`/validation` directory as host bind mounts. No original host source/data trees
were mounted. The delivered bundle itself includes its reference analysis and
videos; the frozen regeneration code computes fresh outputs from native inputs.
The container exited zero and was removed. Package dependency hashes remained
unchanged before and after validation.

All **78,302** compared scientific summary, trajectory-frame and thermodynamic
values match the frozen reference **exactly**: maximum numeric difference zero.
All four trajectories contain the complete 500,000-step/1 ns production run and
1,000 common 1 ps samples. GROMACS/LAMMPS also preserve their initial raw frame.

Independent `ffprobe` decoding verifies all five videos have **1,000 frames,
40 fps and 25 seconds**. Individual clips are 720×720; the synchronized grid is
1440×1440. Camera, alignment, water display crop and playback settings match the
frozen renderer; no frames were interpolated or synthesized.

| Engine | Mean native T (K) | Mean native pressure (bar) | Density from cells (g/cm³) | Native ns/day |
|---|---:|---:|---:|---:|
| GROMACS | 300.01625338 | 5.722313798 | 0.984843463354 | 747.094 |
| NAMD | 300.18529909458 | −8.62001461633 | 0.983338466796 | 412.245 |
| AMBER | 298.32824 | −12.3712 | 0.984548473058 | 733.84 |
| LAMMPS | 299.823145472795 | −1.49560465943 | 0.984620495175 | 28.4705013972 |

These retain the original report's estimator and short-sampling caveats, including
AMBER's native staggered kinetic-temperature definition, native pressure choices,
and LAMMPS's explicitly verified SHAKE setup projection at a closed restart
boundary. No force field, integrator, trajectory or scientific observable changed.

Two harness failures remain preserved. Attempt 01 stopped before analysis because
the capability-restricted image's default root UID could not traverse the private
host output directory. Using its owner UID/GID 1002:1002 fixed this without changing
the image or relaxing isolation. Attempt 02 completed analysis and all five videos,
then the audit incorrectly required exact decimal NAMD DCD endpoint times. The
actual timestamps, 1.0000006290766237 and 1000.000003607501 ps, are identical to the
reference. The corrected read-only post-check uses the existing native-reader
0.001 ps timeline tolerance while still comparing actual values to the reference;
no output was edited or rerendered to obtain pass. Exact helper snapshots and the
original failed receipts are retained alongside the successful postvalidation.

The actual image environment is Python 3.11.2, NumPy 1.26.4, MDAnalysis 2.10.0,
Matplotlib 3.10.7 and FFmpeg 5.1.9. The host reference used Python 3.12/FFmpeg 6.1.1.
The DCD reader's future-version copy-semantics warning remains recorded; it did
not change any measured value. The inherited loopback web-health probe is not a
platform API call and is not treated as readiness of the CPU analysis task.

Regeneration and original post-check took 118.82 seconds. The isolated helper is
[qualify_portable_analysis.py](../qualify_portable_analysis.py); its 21 tests,
Ruff and diff checks pass. Keep both requested output directories new:

```bash
python3 qualify_portable_analysis.py \
  --bundle /path/to/delivery \
  --reference-analysis /path/to/delivery/analysis \
  --output /path/to/new-portable-validation
```

Authoritative passed receipt:
`/home/tux/fs2-alanine-analysis-20260923/final-client-portable-02/qualification-verified.json`
(`306afb295a490bc60757a579373b6f3ab5a2b3df4139cfcd5d9b8219e6b692df`).
