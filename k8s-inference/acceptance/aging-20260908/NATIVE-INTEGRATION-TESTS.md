# Native aging integration checks

Offline integration was frozen on 2026-09-08. The release owner retains all
commit, image-build, Terraform apply and live-acceptance authority. No live
workloads, node settings, quotas or credentials were changed by these checks.

The additive declarations and selected runtime entries bind the two exact
[native image receipts](README.md). `full_catalog` still selects the original
16 archived models. Aging is available through explicit IDs or the `aging`
profile; no CPU formula is represented as an archived B300 model.

## Passing checks

From `k8s-inference`:

```sh
uv run --project components/control-plane python -m pytest tests/test_aging_native_integration.py tests/test_inference_stack.py -q --tb=short
terraform -chdir=stages/workloads test -filter=tests/general_cpu.tftest.hcl -no-color
```

- **59 Python tests passed**: nine native integration cases and all 50 existing
  inference-stack wrapper cases. These bind exact image/fixture/artifact bytes,
  H100-only compatibility, zero-GPU CPU requests and the native admin bootstrap.
- **11 Terraform mock-provider plan cases passed**. The managed PhenoAge case
  evaluates its actual controller/bootstrap ConfigMap: zero accelerators,
  1,000 mCPU and 256 MiB per replica, the existing general CPU queue, and false
  scale-to-zero qualification. Existing static MSA behavior remains covered.
- The release owner separately reported **75 deployment-contract tests passed**,
  including the explicit native CPU/H100 root plan and archived defaults.
- Owned Terraform formatting and `git diff --check` passed.

The packaging regression materializes only the production image's catalog
copies, then loads with its packaged repository as source root. Both native
records load alongside all 16 archived records, with the archived catalog
digest unchanged. The Docker build also executes this isolated augmentation
check against its newly built wheel.

## Findings retained before release

The actual CPU Terraform plan caught a missing qualification projection: it
previously traversed only archived rows. The integration now appends selected
native qualification rows without rewriting the archived rows.

An initial packaging edit to `catalog/runtime/pyproject.toml` changed an
archival helper's bound byte identity and failed the backend regression. That
edit was removed completely; no archive hash or contract was changed. Native
declarations and selected-runtime artifacts are copied by the control-plane
Dockerfile, while exact semantic source mirrors use its existing packaged
repository copy.

The first isolated Python packaging test imported a stale installed development
catalog wheel. It predated the existing OpenFold3 fallback and caused five
test failures. The test now imports the checkout's exact loader, as the
production wheel does; no loader or archival contract was changed to make the
test pass. The full 59-case rerun passed. Unrelated old pytest temporary
PostgreSQL socket cleanup warnings were retained; no such directories were
removed by this lane.

The committed `948b0c01` production image build subsequently failed before its
Python build gate: `COPY` could not find `native` or `deployment-runtimes` in
BuildKit's filtered context. The earlier copy-only test did not exercise
`.dockerignore`, so it was insufficient evidence for build readiness. The
failed release build log remains retained by the release owner.

The correction adds only these two catalog directories to both
`Dockerfile.dockerignore` and `scripts/build_image.py`'s committed expected-input
list. The two exact packaged semantic sources already fall under the existing
`packaged-repository/**` policy; that policy was not broadened. The actual
BuildKit `context-audit` regression now exports both declarations, both selected
runtime entries, both manifests and both semantic sources, verifies exact byte
equality and checks the release wrapper's expected-input coverage. Sensitive
fixtures inside the newly admitted directories and unrelated solution files
remain excluded. All **nine packaging tests passed in 12.27 seconds**, with
Ruff and diff checks passing:

```sh
uv run --project components/control-plane python -m pytest components/control-plane/tests/test_packaging.py -q --tb=short
```

This is a passed local post-ignore context check, not a claim that the corrected
production image has already built or deployed. Root owns that next rebuild.

## Remaining live gate

These are local contract and mock-provider tests, not a deployment or autoscale
measurement. Public HTTP/MCP, independent App settings/history, observed
zero-to-one-to-zero behavior and sibling-feature verification remain separate
release acceptance. New entries keep unmeasured platform and snapshot claims
false; the default initial hot floor is one until an operator explicitly
selects a zero floor for the authorized acceptance.
