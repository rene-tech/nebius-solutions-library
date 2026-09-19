# Ordinary public DiffDock attribution gate

This procedure is prepared, not executed by the dataset lane. The root manager
owns serialized release, owner mutation and scientist06. Keep this separate from
the isolated twelve-request receipt and the24 release176 operations whose
runtime identity is unknown.

## Fixed inputs and ordinary customer path

Use the existing workbench qualification runner and frozen manifest:

- `Q/datasets/diffdock-frozen-public-v1/cases.json`, file SHA256
  `0a1af967ce7506cee2436eaa22ce5b30d22ab46f29852ec3983cda49de857574`.
- Twelve cases: six experimental reference complexes, two seeds, four poses
  per request. Do not replace the inputs or remove scientifically poor poses.
- Existing private scientist identity file, `--only scientist-06 --parallel 1`.
  Use a new cohort/output directory and new runner-generated idempotency keys.
- Preserve the existing06 per-key concurrency limit and all model/output limits.
- Deadline remains `2026-09-19T06:04:00+00:00`; capacity wait is not a software
  failure, but waiting and elapsed times still belong in the report.

From `templates/hcls-librechat/scripts/qualification` in the workbench tree:

```bash
python run_campaign.py \
  --manifest "$CAMPAIGN_ROOT/datasets/diffdock-frozen-public-v1/cases.json" \
  --scientists "$CAMPAIGN_ROOT/private/scientists-private.json" \
  --only scientist-06 --parallel 1 \
  --cohort diffdock-http-identity-public-r1 \
  --output "$CAMPAIGN_ROOT/cohorts/diffdock-http-identity-public-r1" \
  --deadline 2026-09-19T06:04:00+00:00
```

`CAMPAIGN_ROOT` is the existing protected qualification evidence directory. Use
the established evaluator environment; this command admits real ordinary MCP
requests and must not run until root explicitly hands over06 and confirms the
new CP image, four mounted maps and observed owner generation.

## Multi-replica witness

1. Retain before/after Helm release identity, four map hashes, owner generation,
   observed generation/spec digest and ETag. The intended wrapper image is
   `sha256:0c717984c438bb3cac1a139297a06dc39c5fe7fc6ab387c130c0c464a48ba4f9`.
2. Before admitting the matrix, capture at least two Ready DiffDock Pods on that
   image, the managed Service and its EndpointSlices, and node identities.
   If insufficient capacity exists, record the multi-replica gate as pending;
   do not evict work or increase quotas/maxima to force it. Root alone may
   coordinate temporary owner settings within the existing allowed range.
3. For each completed operation, require a non-null immutable Pod UID and node
   UID, gpu_count1 and exactly one GPU UUID. Join the operation Pod UID to
   timestamp-bracketing Pod captures. Verify the same running container ID,
   restart count, desired and actual image IDs throughout execution. An OCI
   index/manifest mismatch needs separately retained registry evidence, not
   string truncation or an assumed equivalence.
4. Verify that the Pod's nodeName maps to the retained node UID; GPU UUID must
   match the contemporaneous node-local observer annotation, not an available
   GPU elsewhere on the node. Keep Service selector/EndpointSlice target UID,
   address and Ready evidence for the serving replica.
5. Require at least two distinct **returned** serving Pod UIDs across the matrix
   to claim cross-replica customer coverage. Two Ready Pods alone is insufficient.
   If routing hits only one, preserve the result and coordinate a bounded
   continuation with root; do not silently claim the multi-replica gate passed.
6. Reuse `verify_diffdock_public.py` against the retained release176-a cohort,
   with the original r6 predeclared0.01Å coordinate/0.001confidence tolerances.
   The validator checks frozen request hashes, actual downloaded artifact
   bytes and matching results. Compare scientific geometry/RMSD separately;
   HTTP success or repeatability does not prove docking accuracy.
7. Record null/incomplete attribution as a failure of this gate even when
   inference succeeds. Missing observer data is not permission to guess. Keep
   old unattributed requests, partial admission failures, warnings and the
   distant low-confidence pose in historical evidence.

The existing minute-by-minute campaign Pod captures can supply conservative
timestamp brackets. `campaign_latency.native_identity` independently joins
operation Pod UIDs to those immutable container records and retains unknown
identity when captures or actual-image equivalence are missing. Do not infer
GPU identities or cold/warm state from the newest Deployment.

After qualification, restore any root-approved temporary replica setting through
the owner API, retaining observed recovery and shared-App availability. Do not
patch controller-generated Deployments. No snapshot policy changes belong here.
