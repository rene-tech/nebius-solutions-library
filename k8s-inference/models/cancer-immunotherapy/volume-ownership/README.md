# Volume ownership on scientific cold starts

A pod that sets `fsGroup` makes the kubelet take ownership of its volumes before
the first container starts. Under the Kubernetes default that is a recursive
walk over every inode, and it happens between "pod scheduled" and "container
started", where no container log shows it. On the shared 128 GiB H100 scientific
cache it cost **153-305 s per Job**, scaling with the cache rather than the
model.

## What ships here

A typed contract, `fs2_serve_catalog.volume_ownership`, for deciding when
`fsGroupChangePolicy: OnRootMismatch` is safe — and **zero callers**.

`FilesystemGroupProducer` and `FilesystemGroupConsumer` state who wrote a tree
and who mounts it, and `assert_authority` refuses the pairing unless they agree
exactly on the group, on the mounted volume root, and on the producer owning
every inode below it. Read-only consumers are not exempt, because `fsGroup`
ownership is applied per volume rather than per mount.

[`volume-ownership-authorities.json`](../../../catalog/runtime/contracts/volume-ownership-authorities.json)
records the state of every `fsGroup`-bearing template, and two guard suites
enforce it in both directions across manifests, Helm templates and Python
renderers. A never-`fsGroup` guard pins supplementalGroups-only for AlphaFold 3
and the academic asset cache, whose delivered modes the ownership pass would
rewrite to group-writable.

## Why nothing is switched on

The check says the platform is not eligible yet, which is exactly why it ships
before any rollout:

- the artifact acquisition Job writes as **gid 10001**, while the model runtime
  pod mounts with **fsGroup 1000** — the volume root cannot match both, so the
  policy would either buy nothing or, once some pod's walk stamps the root with
  1000, let a later pod skip while the producer keeps adding 10001 files that
  then become unreadable;
- the localization Job pins neither `runAsUser` nor `runAsGroup`, so the group
  its staged tree ends up owned by is not stated anywhere;
- the shared many-writer cache has no single producer at all, and additionally
  needs a sequenced one-time adoption with a terminal receipt and stage
  admission binding — controller work, not renderer work.

No rendered manifest changes in this commit. Aligning those identities is a
deliberate change to running services and is tracked under `follow_up` in the
register.

## Measured on real H100

[`evidence/h100-canary-20260903.json`](evidence/h100-canary-20260903.json) is a
**measurement of the policy's effect, not an acceptance of any shipped
artifact**, and it should not be read as one:

- The volume root was already adopted before the first trial, so all three are
  **warm-root** measurements. They say nothing about a first adoption.
- The checked-in Mosaic plan is explicitly non-runnable, so the applied
  manifests were derived out of band from it. The rendered plan inputs are
  recorded by SHA-256; the post-adapt manifests were not retained.
- They ran against commit `f09b1f5a`, which is **superseded**. The library was
  narrowed afterwards and no longer renders a bootstrap Job at all.

Three concurrent Mosaic trials on `k8s-inference-h100` in
`project-e00rene`/`eu-north1`, same claim and nodes as the recorded baseline:

| Stage | Baseline, no policy | Warm root, `OnRootMismatch` |
| --- | --- | --- |
| Mosaic design, 1×H100 | 154 s, 153 s, 305 s | 2 s, 1 s, 2 s |
| Mosaic aggregate, CPU pool | 171 s | 1 s, 1 s, 1 s |

Container time was unchanged at 216-226 s against a 214-217 s baseline, all
three passed the canonical adapter validator plus the structural binder gate,
and results stayed owned by `fs2:fs2` with a separate pod reading every
committed artifact.

## Defects the live run and review caught

The first bootstrap draft reported success while leaving the root at mode `775`
and re-adopting on every pass: it had dropped `CAP_FSETID`, without which the
kernel silently clears `S_ISGID` on a chmod of a directory whose group the
caller does not hold. Review then found three more in the same renderer — `find
-exec chgrp`/`chmod` dereferenced symlinks so a link in the cache could rewrite
a target outside it, the adopted root could be a descendant the kubelet never
checks, and nothing ordered the bootstrap before the stages that depended on it.
All four are why that renderer is follow-up work rather than part of this patch.
