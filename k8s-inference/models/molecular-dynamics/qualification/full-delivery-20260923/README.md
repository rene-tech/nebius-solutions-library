# Complete canonical four-engine deliverables

The [receipt](receipt.json) records the frozen, complete scientific handover.
The original [file inventory](delivery-manifest.json) hashes all 548 files:
1,525,211,038 bytes excluding the manifest. The gzip archive is 949,216,375 bytes;
GNU tar compared every archived member against the frozen directory successfully.

Server directory:
`/home/tux/fs2-alanine-comparison-20260923/delivery-02`

Archive:
`/home/tux/fs2-alanine-comparison-20260923/four-engine-alanine-ff14sb-tip3p-20260923.tar.gz`

Archive SHA256:
`befe9c41b99fd1e16769bd9aba2df9c9df3303d38a879f715401cf984f163809`

Extract into a new directory, then run:

```bash
python3 delivery-02/delivery_manifest.py delivery-02
```

The full bundle contains native raw outputs, inputs and converters, commands and
software identities, independent topology/static-energy checks, complete common
analysis, four clips, the synchronized 2×2 clip, and the phi/psi plots. Start with
`README.md`, `RESULTS.md`, `ACCEPTANCE.md` and `REPRODUCE.md` in the bundle.

See the tracked [acceptance matrix](../../comparison/qualification/ACCEPTANCE-20260923.md)
and [result table](../../comparison/qualification/RESULTS-20260923.md) without
downloading the raw archive. These explicitly retain numerical differences,
fixed-seed repeatability versus independent ensembles, native pressure/temperature
conventions and the limited scope of GPU-process snapshot qualification.

The full archive does not fit in Rene's current remaining workspace capacity.
The separate 31 MiB preview contains reports, input files, plots and videos,
not the raw trajectories or complete portable regeneration package. No existing
workspace data was removed and no quota was raised. The workbench replacement
and preview publication are tracked separately; this artifact receipt alone is
not proof of their deployment.
