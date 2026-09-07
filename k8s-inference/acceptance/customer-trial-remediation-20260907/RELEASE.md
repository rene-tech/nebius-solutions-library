# Integrated H100 remediation release

Deployed source: `5fec520596b6aa9b8a2eb1194d742f0c35fde575` on solutions-library
`main`. Harness/evidence preparation: `1d6cff9f3fbf0f71ee5cedf41c469e08c1348209`.
Both were pushed to `rene-tech/nebius-solutions-library` before live testing.

| Component | Deployed digest |
|---|---|
| Control plane, model controller, scientific companions | `sha256:6b6cdb1f0da4b63c312acb1237949588b7075421f7cb0bbfc2eee286639e317f` |
| Admin console | `sha256:9ac15924a585d7d73ec6dd668e5ede350103872f561e5b432f34b3745ff2e6c8` |

Both images were built from the exact committed source and published in the
existing regional registry. The only customer `terraform.tfvars` changes were
these image pins and matching admin provenance. No model runtime/capture,
scientific fixture, pool size, hot floor, queue quota, priority or driver changed.

The staged Terraform workflow validated and regenerated all downstream inputs,
including the new per-node scheduling envelope contract: `h100-1x` has 15,900m
allocatable CPU; `h100-reserved-8x` has 127,900m. These facts already existed in
the customer's configuration. The additional scientific controller `pods/log`
reader is namespace-scoped and reads the existing snapshot startup marker.

At 16:08:48 UTC the post-apply infrastructure, foundation and workloads plans
each contained **zero managed resource actions**. API/admin/model-controller
Deployments each had two ready replicas with the expected image and no new
restarts. Public discovery/admin inventory retained all 24 required configured
models; GLM remains explicitly excluded. See [inventory](deployed-inventory.json).

Private post-apply plan JSON hashes (state/plan contents are not published):

- Infrastructure: `7605e72d04c1348e5cdb339537b806a1d7e3651ef05cb0e3957ad9e2a3d31f6b`.
- Foundation: `8361bfce5cf68426ec892cb641432ba1ff7c48a822c68ce23d5ced38491fd128`.
- Workloads: `800b57138296c5c0bb4a8138023f9ce864b6c9a3589eb5563eb28e65c709b0fe`.

The first packaging-only plan attempt used the admin image build archive, which
contains only `k8s-inference` and therefore omitted shared library modules. It
failed before infrastructure changes. Deployment used a full repository archive
of the same commit instead; no Terraform source workaround was needed. The
failed log is retained privately as `5fec5205-cohort-plan-incomplete-archive.log`.

## Verification before the full customer cohorts

- Complete non-external backend suite: **1,594 passed**, four optional skips;
  75 external-service tests deselected, one existing Starlette deprecation warning.
- Isolated PostgreSQL: **19 passed, no skips**, including the actual enabled
  admin lifecycle join, ledger/bridge replay and scientific dispatch policy.
  The disposable local test database was removed; production data was untouched.
- Complete admin UI suite: **149 passed**; TypeScript passed.
- Placement/deployment/scheduling: 184 focused backend tests, 161 root tests,
  and 31 Terraform module/workload rendering tests passed (these overlap broader
  suites and must not be summed into a unique total).
- Ruff, focused strict typing, root/workload Terraform validation and runtime
  recipe identity checks passed. Qualified shared runtime/controller sources
  remain byte-for-byte unchanged.
- Live admin preflight: real structure downloads verified, historical artifact
  freshness and numeric GPU partitions corrected, four snapshot options visible.
- Dedicated public Qwen preflight: **36/36 requests succeeded**, no retries or
  hot readiness losses. No burst replica appeared, so the live hot-plus-starting-
  burst regression remains **coverage-not-observed**, not a claimed pass.

These release/preflight checks do not replace the two full customer cohorts.
Their scientific results, background availability and active-browser checks
determine the final experience verdict.
