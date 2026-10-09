# Compatible reader bridge — 5 October 2026

This release makes every shared Scientific AI reader understand the additive
RDMA execution fields **before** a ninth MPI execution shape is published. Its
catalog still contains the existing eight shapes. The database ledger stays at
38. No capacity, queue quota, customer policy, model floor or Gateway changes.

The immutable image is built from committed source
`dbcb4b994caf3c2b1847a6b94d078a5bf98d37d5`, not the concurrently edited worktree:

```text
cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:872b7d58cf275f2bc7e7d396e626fd1418285e65f25047ed8fbd35ab7af9f6dd
```

Packaged catalog digest: `3e62475ce484705dfb28f0f568b5d6311956c645cc51a4f10030cbb9716c9ecc`.
Scientific profile digest: `5799f04002c25451bf922058d6b35ae07fbf41a830a87c7c3ee788e802d354dd`.
The OCI revision/tree labels and packaged 38-migration inventory are verified.

`activate_bridge.py` updates only the API main/init/future-tools image, controller
image, maintenance image and the previously reviewed H100-single scheduling
metadata correction. The latter reflects eight existing one-GPU nodes and
already-existing live Kueue resource values; it does not raise a quota. Its
immutable ConfigMap is `fs2-scientific-scheduling-0ae63eb7efec`.
All other API/controller ConfigMap references, resources, rollout settings and
schedules are preserved. Image/catalog/profile/scheduling annotations are made
truthful. The helper defaults to client/server previews, records exact inverses,
and tests identity/configuration before writes.

One initial activation preview rejected a routine maintenance CronJob status
resourceVersion race before any write. A fresh guarded preview then applied
successfully. The helper now guards that CronJob by UID and full spec, so normal
scheduled status changes cannot invalidate the configuration check; real changes
to its schedule, suspension or template still do.

`verify_bridge.py` requires exactly three API and two controller Pods on this
image, no old/terminating readers, the exact packaged catalog/eight-shape profile,
a successful new scheduled maintenance Job, unchanged workshop and queue specs,
and the original active Lynx Pod UID with zero restarts. Authenticated discovery
is additionally tested through the public origin with the existing system/qa
identity using `../gromacs-mpinat-20261003/verify_api_release.py`.

Private build provenance, previews, failed preview, forward/inverse patches and
read-only receipts are retained under:

```text
/home/tux/secure-handoff/fs2-lynx-longrun-20261005/release/bridge-rdma-reader/
```

This reader barrier is **not** a claim of completed customer workflow acceptance.
Concurrent large-checkpoint recovery, multi-node peer loss and final RDMA
publication/qualification belong to the parent acceptance task.

Verified at 19:17 UTC: all five deployment readers passed; authenticated public
discovery returned HTTP 200; maintenance Job `fs2-serve-control-plane-maintenance-29853796`
completed successfully at 19:16:16 UTC. The preserved Lynx Pod remains Ready,
UID `28cba283-f42d-4fe7-aac7-010ba2222f1e`, with zero restarts. All three packaged
MPI catalogs still have eight shapes. Actual queue and workshop specs are
unchanged. Forward/inverse receipts are in `activation-r2/`, deployment/sibling
proof in `verified/`, and authenticated discovery in `after-public/`.

25 focused bridge/cardinality-helper tests and Ruff passed. The bridge barrier
was handed back to the parent before any final RDMA publication.
