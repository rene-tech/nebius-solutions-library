# ESMFold2-Fast adapter

`esmfold2-fast` is a separate distilled model and backend identity. It uses the
same canonical scientific request envelope as other batch models, but its
model-specific parameter validator does not admit an MSA or templates and
rejects either field instead of silently ignoring it. Its canonical workload
profile places CPU normalization before the single-GPU folding stage.

Artifact inputs are storage-independent `artifact-pointer/v1` values plus
logical names resolved by the controller. This is an image-build and
H100-qualification input only; it neither aliases `esmfold2` nor creates a live
route.
