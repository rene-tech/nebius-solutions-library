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

## Ordered input materialization

The public H100 acceptance found a second startup cost after replacing large
CPU-stage images: tiny (4–8KiB) inputs took 1–3s to materialize but incurred up
to 62s between separate init containers on a newly provisioned CPU worker.
Multiple inputs now run through `scientific-materialize-many` in a single
init container. It parses the same ordered argument lists and calls the same
artifact materializer, retaining digest, size, media-type, archive, and scoped
capability checks. There is no parallel-write race between overlays. A failure
stops subsequent entries; normal retry semantics remain unchanged.

One input keeps its existing `scientific-materialize` command. Multiple inputs
reuse one HTTP client, the same workspace and the same 100mCPU/256Mi requests
and 1CPU/1Gi limits. Per-input completion logs retain logical artifact ID,
expected bytes and elapsed time. HTTP library request logging is not used
because presigned object-storage handles should not be copied into those logs.

This eliminates extra init transitions, not CPU node acquisition or model
loading. Operators who value presentation latency can keep one `batch-cpu`
node warm through `deployment.cpu_pools["batch-cpu"].autoscaling.min_nodes`.
Changing that floor does not require increasing its ceiling or any quota.
Live post-release timing remains a separate acceptance check.
