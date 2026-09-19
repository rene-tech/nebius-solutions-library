# Open-runtime response attribution — Q63

The two public release176 DiffDock cohorts completed 24 requests, but their
operation runtime Pod/node identities were null. The stdlib wrapper did not emit
the existing three-header protocol, and a service with multiple ready replicas
cannot be attributed by selecting an arbitrary Pod. Those historical results
remain unattributed; this repair does not backfill a guessed identity.

## Narrow change

- `common/server.py` derives response Pod UID from `FS2_RUNTIME_POD_UID` and
  operation/attempt from the unique matching gateway headers. Missing, duplicate
  or invalid identity inputs omit the hints without changing inference. Incoming
  response-identity headers are not reflected.
- The control plane recognizes explicit `http-open-runtime-v1` instrumentation.
  It verifies the same GPU container's exact wrapper command, downward API UID,
  actual/spec image, model revision, listener, Service and EndpointSlice. The
  existing ASGI and Cosmos verification paths retain their checks.
- The optional wrapper identity applies to successful and error JSON responses;
  no body/schema, retry policy, health probes, request/concurrency limits, seeds,
  precision or model code changes.

The pinned base is DiffDock image
`sha256:9766b4fb2a22787874bd8d90306980a4cb1ff9808941f0f1ae429d8b8f7cc948`.
Read-only OCI config inspection verified Entrypoint
`["python3", "/opt/fs2/runtime/common/server.py"]`, Cmd `null`,
`FS2_MODEL=diffdock`, amd64/Linux and user `10001:10001`.
`models/structure/runtime/common/Dockerfile.response-identity` copies only the
wrapper over that exact image, preserving all other layers and inherited config.

## Prepare; do not silently promote

```bash
cd k8s-inference/components/control-plane
uv run --frozen pytest -q \
  tests/test_open_http_response_attribution.py \
  tests/test_runtime_response_attribution.py \
  tests/test_cosmos_response_attribution.py \
  ../../models/structure/runtime/common/tests/test_server.py \
  ../../acceptance/open-runtime-attribution-20260919/test_candidate.py

uv run --frozen python ../../acceptance/open-runtime-attribution-20260919/prepare_candidate.py \
  --configmaps /protected/current-baseline/configmaps.json \
  --owner /protected/current-baseline/diffdock-modeldeployment.json \
  --image registry.example.invalid/diffdock@sha256:<built-wrapper-image-digest> \
  --output /protected/new-attribution-candidate
```

Use the actual captured `ModelDeployment` object or admin desired revision
(including its ETag), not a reconstructed operator spec. The helper is offline,
refuses stale owner/template identities, and writes
an explicitly unqualified proposal in a new directory. It does not edit source
catalogs, maps, existing templates, owner state or Kubernetes resources.

Baseline177 still registers the historical template image `471db264…`; the
operator-selected owner image is `9766b4fb…`. The helper supports that deliberate
image override and produces a new immutable template without overwriting the old
one. It preserves placement, replica settings, timeouts, resources and queues.
Historical snapshot records remain untouched, but the new owner proposal has
`snapshotPreference: Never`; no old process snapshot qualifies the new wrapper.

The root manager owns image build, exact-image H100 numerical regression,
four-map/runtime-record and admin bootstrap checks, serialized rollout, ordinary
public requests across two replicas, and sibling acceptance. A CPU test or
prepared proposal is not live qualification or readiness. New digest-specific
semantic evidence must be recorded, not copied from the old image's receipt.

Run `common/tests/test_native_adapters.py` in a separate pytest process: its
pre-existing module-level NumPy stub otherwise contaminates `pytest.approx` in
the lifecycle suite (`numpy.isscalar` is absent). The initial combined-suite
failure is retained as test-harness interference, not hidden as a runtime pass.

## Exact-image evidence and prepared promotion

Wrapper index `0c717984c438bb3cac1a139297a06dc39c5fe7fc6ab387c130c0c464a48ba4f9`
passed twelve actual HTTP requests on an H100. All48 generated 3D poses matched
the frozen r6 first-pass coordinates and confidence values exactly in this
ordered pass. The portable receipt is
`models/structure/runtime/common/qualification/diffdock-http-h100-20260919.json`.
This is not general byte-identical reproducibility, experimental affinity,
public replica attribution or snapshot qualification.

For new uniquely named isolated resources, use `kubectl create -f manifest.yaml`,
not client-side apply: its last-applied annotation duplicates the large reference
ConfigMap and exceeds the Kubernetes annotation limit. The original partial
apply (Pod created, ConfigMap rejected) and recovery by creating only the exact
missing ConfigMap are retained in `launch-recovery.json`; no limits or test
inputs were changed and the Pod was not recreated.

`prepare_promotion.py --baseline CAPTURE --isolated RETAINED_TEST --output NEW_DIR`
checks immutable GPU receipts, both Kubernetes/admin owner specs, all current
owner compatibility, candidate rendering, actual Registry.load and admin
bootstrap. It emits four immutable maps, a minimal values delta, complete
preserved values, rollback values/owner, and the successor descriptor. It never
applies resources. The new template records the wrapper command, downward Pod
UID and explicit `http-open-runtime-v1` annotation; historical templates and
snapshot records remain unchanged. Snapshot preference stays Never.

Before root applies a serialized release, recheck the captured ETag/map refs and
render the full Helm chart. A later release must rebase the candidate if unrelated
values changed. Root must still run ordinary scientist06 requests while at least
two exact-image replicas are Ready, retain EndpointSlices and contemporaneous
Pod/node/GPU observer identities, and verify returned operation identities match
the actual serving Pod. Do not infer success merely from two Ready replicas: at
least two distinct response Pod UIDs must be witnessed to claim cross-replica
coverage. Preserve the24 historical unattributed operations without backfilling.
