# GenMol requested-yield repair

The scientific cohort observed a successful response containing fewer molecules
than requested (`native-r3/scientist-07/genmol-10-20-logp-n16-r3`). The previous
adapter made one upstream call, discarded chemically invalid candidates and,
when requested, canonical duplicates, then reported any nonempty output as
success. The pinned upstream sampler also filters failed SAFE-to-SMILES decodes
before returning its list.

`generation.py` now requests additional samples only for the missing count.
Sampling is bounded by eight calls and eight times the requested candidate
count. The original temperature, noise, scoring and midpoint minimum-mask length
are retained. This is rejection sampling, not a new optimizer. Success requires
exactly the requested number of valid molecules and, when `unique=true`, distinct
canonical SMILES. With `unique=false`, genuine repeated model samples remain
allowed; the adapter never pads by copying prior molecules.

Successful results report requested, accepted and returned molecule counts,
sampling attempts, requested/returned candidate counts, upstream omissions,
invalid molecules, canonical duplicates and non-finite scores. Exhaustion returns
HTTP 503 with structured `generation_exhausted` detail and the same counters;
partial accepted yield is not labelled success. On exhaustion, returned count is
zero and accepted count describes discarded partial work.

Important existing protocol limitation: the adapter maps the mask range midpoint
to upstream `min_add_len`. Upstream chooses token length from its empirical
distribution with this minimum. Neither the requested range nor that value is a
heavy-atom-count range, and no maximum molecular size is asserted.

CPU verification: ten focused tests in `test_generation.py` cover invalid and
duplicate replacement, short upstream batches, preserved sampling parameters,
unchanged LogP scores, non-finite scores and bounded exhaustion. Both Dockerfiles
copy the new helper; source compiles and `git diff --check` passes. This document
is not a live qualification: image build, deployment, repeated real GPU batches,
and customer-visible exhaustion handling remain the campaign manager's gates.

Primary implementation reference: [pinned NVIDIA GenMol sampler](https://github.com/NVIDIA-BioNeMo/genmol/blob/add09fc83b7255bd09c797e527c0f4b51f5fb7c1/src/genmol/sampler.py).
