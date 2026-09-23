# Native LJ starter

This 4,000-atom reduced-unit example demonstrates native include files, ordered
preparation/production/analysis, trajectories and complete continuation context.
It is not a material prediction or an equilibration/convergence prescription.
`51000` is the absolute target: 1,000 preparation plus 50,000 production steps.

From this directory, package only the native files:

```bash
tar -C inputs -czf input.tar.gz .
```

Submit `request.json` and the bundle through the hosted LAMMPS workflow interface
once that exact App release is activated. `customer-bucket` requires the tenant's
bucket to be linked and authorized by the platform; no credentials belong inside
the input bundle. For local qualification, set `output_destination` to
`platform-artifacts` and use the runtime invocation in the model README.

Native syntax is preserved. To use a supplied data file or potential, add it to
the same bundle and reference its relative path in the input script. Preserve
all files needed by `in.resume`; a binary restart alone is not a full recipe.
The example writes distinct trajectory parts, so a committed part is not
silently replaced on continuation. Download logs, every trajectory part,
`state.restart`, `final.data`, input files and the result provenance together.
LJ time is reduced time, not ns; no ns/day conversion is meaningful here.
