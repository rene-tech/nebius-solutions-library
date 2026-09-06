# Lightweight scientific CPU stages

Scientific workflow stages normally use the model's qualified runtime image.
For RFdiffusion `collect` and Mosaic `aggregate`, that meant pulling 4.15–4.20 GB
of GPU libraries onto a CPU node to execute about one second of Python work.
The H100 acceptance campaign on 2026-09-06 measured 165 and 200 seconds of image
pull time respectively. Those times are image pulls, not weight localization.

The execution map can explicitly set `image_role: "scientific-tools"` for a
CPU stage. It then uses the same regional, digest-pinned control-plane/tools
image that the stage's prepare and collector containers already require.
RFdiffusion's collection command needs only Python; Mosaic's tools image
packages its unchanged `runtime_entrypoint.py` and an aggregate-only launcher.
Inference stages and output validators are unchanged.

Two identities remain distinct:

| Identity | Meaning |
| --- | --- |
| Frozen stage `image`, `FS2_STAGE_IMAGE_DIGEST` | Image that actually executes this CPU stage |
| `model_runtime_image_digest`, `FS2_RUNTIME_IMAGE_DIGEST` | Qualified GPU runtime that produced the model outputs being finalized |

Both are persisted with the accepted execution plan. Updating the tools image
after admission does not substitute a new image into an existing run. Legacy
records without the optional model-runtime digest retain their original
single-image interpretation. Do not downgrade controllers while new-format
accepted runs remain active; older binaries do not recognize the added field.

To add another lightweight stage, first verify its complete command and
dependencies in the tools image, then opt in that CPU stage and refresh the
scientific recipes. GPU stages cannot use this role. Keep upstream preprocessing
and filtering in their existing model image when they depend on those packages:
in particular, this change does not replace Proteina filtering or ESMFold's
upstream input serialization with simplified implementations.

Post-deployment benchmark results are recorded separately from the baseline;
the code change alone is not evidence of an improved live startup time.
