# SAI-23 Cosmos media reference remediation — additive r02 candidate

Status: **SOURCE CANDIDATE ONLY — INTEGRATION/LIVE NO-GO**

This note is the durable handoff for the additive successor to rejected commit
`b31b14401db3067fe7a7a6df7b71bd739041272e` (tree
`03e7c85b774620f20a33ce8979c627586781e061`). That exact candidate remains
preserved. Independent review found that it protected only new admission,
retained an unsafe 1.125 GiB declared input envelope, did not contain the real
pre-merge Cosmos media feature, and did not traverse local schema references or
conditional branches.

No test, build, formatter, linter, package manager, scanner, container, CI,
Terraform, Helm, cloud, cluster, database, registry, credential, deployment,
cleanup, or live command was run while authoring r02. The tests described below
are source additions only and have not been executed.

## Exact feature provenance and supported surface

The real pre-merge feature was inspected read-only at commit
`722fdc7239ca6c8d42831a58e56ad8c99adf440a` (tree
`c0fc71e8c5e71e7336251164ccbb2bb2831057c9`) on branch
`agent/fs2-cosmos-stockholm-remediation-r20260917`. Its larger 131-file change
set was not merged or cherry-picked because it includes unrelated work and
deletions. R02 additively reconciles only the qualified Cosmos behavior:

- `text-to-image`
- `text-to-video`
- `image-to-video`
- `video-to-video`
- `transfer-video`, including edge, blur, depth, segmentation and WSM controls

The public contract and embedded adapter now agree on those modes. Image and
video source fields and explicit control references accept finalized platform
`ArtifactRef` objects at admission. The worker materializes them immediately
before dispatch. The embedded adapter accepts only bounded base64 `data:` URLs;
it has no caller-selected HTTPS or filesystem-path alternative. Text-only image
and video requests remain available without artifact inputs.

## Layered fail-closed enforcement

1. Admission rejects all non-artifact values in `input_reference`,
   `vision_path`, and `controls[].reference`. This includes
   `https://10.5.0.1/` and public HTTPS URLs alike.
2. Every claimed Cosmos operation is checked again after its durable request is
   read. This covers rows admitted by an older replica, queued work and retries.
   A violation becomes a terminal `artifact_input_invalid` operation before
   `runtime.invoke`; the retained row is not deleted.
3. Aggregate declared size is checked before the materializer opens any artifact
   content stream.
4. After materialization and immediately before invocation, all Cosmos media
   positions are checked again. Only a media-type-qualified, bounded `data:` URL
   can cross the runtime boundary. A missing/disabled materializer therefore
   fails closed.
5. The embedded adapter repeats the data-URL, decoded-size, MIME and file-magic
   checks. Transfer controls use a bounded, pod-local `emptyDir` shared only by
   the adapter and model container; generated filenames are server-selected.
6. Artifact-delivery MP4 responses remain binary through the adapter. The
   control-plane runtime accepts that native binary response only for Cosmos,
   only for MP4, and only when the adapter's byte-count and SHA-256 identity
   headers match; the existing serving-output artifactizer then stores it and
   returns the tenant-scoped result reference.

The generic schema hardener resolves escaped local JSON pointers and walks
`$defs`, legacy `definitions`, objects, arrays/tuples, combinators, and
`if`/`then`/`else` plus related conditional/applicator keywords. Non-local or
unresolved references fail contract construction.

## Bounded memory envelope

The rejected declaration allowed one 512 MiB primary input plus five 128 MiB
controls. R02 reduces this to:

| Input | Per-field limit |
| --- | ---: |
| Primary image/video | 24 MiB |
| Each explicit control | 4 MiB |
| Combined request | 32 MiB |

The aggregate check runs before stream access. Materialization remains an
in-memory data-URL bridge because the selected runtime accepts multipart media,
not platform artifact identifiers. At the 32 MiB aggregate limit, base64 is at
most about 42.7 MiB. A conservative static peak model allows roughly 192 MiB per
control-plane worker for raw bytes, bytearray/bytes conversion, base64 bytes and
text, the materialized object, and final JSON bytes. Four configured workers
therefore reserve about 768 MiB of the 2 GiB limit, leaving about 1.25 GiB for
the interpreter, ASGI stack, encrypted row handling, and variance. The adapter
has one active generation and the same 32 MiB aggregate, and its transfer spool
is capped at 32 MiB.

These are design bounds, not measured memory results. Execution was forbidden
for this source-only lane. Any later integration review should measure peak RSS
under four concurrent maximum-size materializations before accepting the chosen
limits.

## Authored regression coverage (not executed)

- Exact schema coverage for all five qualified modes.
- Admission rejection of `https://10.5.0.1/` in every external-locator field.
- Escaped field names, escaped `$ref` tokens and conditional schema branches.
- Retained unsafe row: terminal failure, zero runtime invocations, row preserved.
- Aggregate declared-size rejection both at admission and before stream open.
- Real primary image/video and transfer-control artifact materialization paths.
- Embedded adapter DTO rejection, decoded content/MIME checks, multipart
  dispatch and bounded shared control spool wiring.

## Unresolved program gates

R02 must not be integrated or deployed on its own:

- **SAI-03:** model-pod egress remains an independent defense-in-depth gate.
- **SAI-18:** same-tenant principal isolation is required before tenant artifact
  ownership is sufficient for this feature.
- **SAI-19:** materialization currently compares stored public metadata but does
  not recompute SHA-256 while reading. Digest verification remains required.

Any future authorized rollout must put the rejecting runtime adapter in place
before a control-plane replica can publish the media contract, verify the
Recreate rollout has no older Cosmos adapter, and then replace or drain every
older control-plane worker before enabling new media admission. This ordering,
plus SAI-03 egress, is the mixed-version boundary for rows admitted by legacy
replicas; r02 contains no rollout evidence.

No integration, release, deployment, rollout, live verification or GO decision
is claimed here. Rollback for this unintegrated source candidate is simply to
leave the task branch unmerged; no runtime state or resource was changed.
