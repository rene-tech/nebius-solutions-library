# ESMFold2 adapter

`esmfold2` is the full, MSA-capable ESMFold2 identity. Its model-specific
validator consumes the canonical `scientific-run-request/v1` envelope and the
controller expands CPU input preparation before one-GPU folding. The request
names only a canonical input manifest; storage locations and runtime commands
are controller-owned.

The GPU stage requires the full trunk, ESMC-6B closure, and CCD bundle. Its
40 GiB shared cache exceeds their combined expanded size. Production runs are
fixed at 20 recycles and 200 sampling steps; reduced smoke tuning is never an
implicit default.
