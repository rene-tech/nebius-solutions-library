# AmberTools preparation and analysis layer

This is the open-source AmberTools26 suite, **not** PMEMD CUDA and not an NVIDIA
NIM. The private PMEMD26 engine is built separately from the operator-approved
academic source. The final engine bundle must pin both components before its
worker identity and hosted scientific qualification are generated.

The official [AMBER download instructions](https://ambermd.org/GetAmber.php)
support `conda-forge::ambertools=26` for preparation and analysis. This variant
does not provide CUDA or CPU-parallel AmberTools execution; GPU simulation uses
the separately compiled PMEMD SPFP/DPFP binaries. See the
[AmberTools26 inventory](https://ambermd.org/AmberTools.php).

`Containerfile.resolve` is an acquisition step, not a reproducible release.
Export its complete explicit package lock and package inventory, then build the
release layer from that lock. Keep the `/opt/ambertools` installation prefix
unchanged when composing the private engine bundle. Do not globally replace
the PMEMD `AMBERHOME` or Python interpreter: tool stages select this tools prefix
explicitly while PMEMD stages retain their own engine environment.

Qualification must include real LEaP preparation and ParmEd/CPPTRAJ analysis of
readable native coordinates/trajectories, complete output preservation and error
propagation. Help output and the presence of executables alone are not evidence
of scientist readiness. Additional installed programs are inventory, not a claim
that every scientific method or accelerator mode has been qualified.
