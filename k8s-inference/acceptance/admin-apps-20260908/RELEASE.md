# Apps-first admin release — 2026-09-08

Status: the Apps-first release is deployed and its corrected serving/scientific
clone workflows pass live acceptance. Initial failed attempts remain documented.
The subsequent AltumAge/clinical PhenoAge and CPU-managed-App extension has its
own [release and live acceptance record](../aging-20260908/RELEASE.md); it is not
retroactively included in this original release's acceptance claim.

## Exact source and artifacts

- Repository: `rene-tech/nebius-solutions-library`, branch `main`.
- Runtime source: `d21439d025c806f6a3bcb4167c57b95d84ff923f`.
- Source tree: `d661427edc25fd4f4c45c7542f4ba3cd0c78c0dc`.
- Registry prefix: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/`.
- `fs2-serve-control-plane`:
  `sha256:09285d25fea2b319886e933fbf5ab63d5fcd1a875b7c793c27b0efe7cd52b65b`.
- `fs2-serve-admin-console`:
  `sha256:50629c0274dc6a5b751e1e36224449186130945a91d90de8be1110cc44f041d8`.
- Admin CycloneDX SBOM SHA-256:
  `da5cae5f3137ec4b75e30d801b7998109b9c0238ac7ffca7b9c9875249dc85ca`.
- Unchanged package-lock SHA-256:
  `1d45f7fa8912c7801a02d02ecfb75c6c69281ac1ce6ea5135851250a547eda45`.

Both artifacts were built from the committed source and published by the existing
release workflow. The rollout archive includes the same commit's shared root
`modules/`, alongside `k8s-inference/`; these modules are reused, not copied into
the inference solution's source.

## Scope and target

Cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`, region
`eu-north1`, context `k8s-inference-h100`. Admin origin:
<https://89.169.99.188/admin/>. No capacity limits, GPU node-group settings,
qualified scientific recipes or drivers change in this release.

The customer tfvars update pins the two images and admin provenance. Terraform
also owns the additive ModelDeployment app-identity schema, Loki connection and
new database migration contract. Existing model desired state is preserved by
the create-only bootstrap, including settings saved later through the console.

## Local verification

- UI: 192 tests; TypeScript and production build passed.
- Backend Apps/identity/controller focused suite: 120 passed.
- Real PostgreSQL suite: 73 passed, including independent clone admission,
  idempotency, canonical upstream payload and runtime-role access.
- Helm and PostgreSQL release contract: 139 passed.
- Ruff: all backend source and tests passed. New/changed owned modules passed
  strict typing. Existing unrelated typing errors in `lean_routes.py`,
  `deployment_runtimes.py` and qualified `scientific_batch/execution.py` were not
  expanded into this task or represented as a full-project typing pass.
- Workload Terraform validation and formatting passed.
- Final full backend suite: 1,687 passed, 4 skipped, 87 deselected (264.19s).
  PostgreSQL and object-storage marked suites are separate; PostgreSQL passed
  above. Existing optional skips are not claimed as exercised.

Counts overlap; these are not additive test totals. The earlier full backend run
passed 1,684 tests with one release-pin mismatch; chart defaults, schema and
validation now use the canonical 28-migration contract. The isolated SQL fixtures
were updated for the latest migration and actual scientific request owner.

## Retained failed attempts

- Initial Terraform archive omitted the shared root modules, so infrastructure
  initialization failed before any cloud mutation. Re-extracted `modules/` from
  the exact same commit and restarted planning; no Terraform code workaround.
- Browser namespace preflight initially probed an unrouted `/livez` and received
  404. Correct public `/admin/` HTML and normally verified TLS succeeded. This was
  a test-path error, not evidence of an unavailable admin service.

Private build, plan and browser evidence lives under the existing H100 release
directory. Credentials, cookies, issued keys and model payloads are not included
in this public report.

## Previous release / rollback reference

Previous runtime source: `c85aa26e46f84ca5ae0a85b2454e86522b65cead`.
Previous control plane:
`sha256:8c05c7a62e439ad01cb982976c1ce6203596d3b27647a67e21bb5d9792af2964`.
Previous admin:
`sha256:0f2b63c40d67c778cb2fc272a7eb482bc170b786ab257fbebe0461d7658291bc`.
Retain the old release receipts and Terraform inputs. Database migrations are
additive; do not remove new tables or accepted app/run history during rollback.

## Live acceptance

The initial Terraform deployment succeeded and both API/admin/controller
workloads became Ready. A bounded ten-minute public sampler recorded 112
successful admin/discovery samples and no failed samples. This is sampled
availability, not an uninterrupted availability SLA.

See `BROWSER-R01.md`, `SCIENTIFIC-APPS.md`, `SERVING-APP-PUBLICATION.md` and
`USERS-CAPACITY.md` for retained live failures and partial successes. Actual
browser history, five resource/concurrency charts, Loki logs, container details,
independent serving-app creation/scaling, artifact download and key revocation
passed. The first serving-clone inference returned404 and scientific-clone
Settings returned503, so this initial release did not pass acceptance.

## Corrective backend release

Commits `e885520d31ecc837e0bacbdeade6f2ea82642a9a` and
`2d170292037386f339fdc96fcf115a07adcf6932` correct:

- Cross-replica scientific app inventory refresh before admin policy projection.
- Exclusion of artifact-upload bookkeeping from logical App/User run counts,
  lifetime last-use and Runs pagination; raw operation history remains intact.
- Capacity SQL's JSON array alias collision with the integer operation attempt.
- GPU chart attribution for a still-running reusable worker after individual
  request intervals end. Current observed GPU ownership also covers idle time;
  terminated Pod annotations cannot reopen historical allocation intervals.
- Kubernetes-valid Deployment names longer than63 characters in observed
  status and startup-retention queries. The live70-character test Deployment
  stays unchanged; Service and real hostname validators retain their rules.

The `e885520d` intermediate image was built but not deployed; the final backend
build includes the name fix at `2d170292`. The unchanged admin image remains
from `d21439d0`. Follow-up exact-commit checks: 74 real PostgreSQL tests, 73
combined App/User/Capacity API tests, 87 focused naming/bridge/startup-retention
tests, and owned-module strict typing/Ruff passed. Counts overlap. Final full
suite passed **1,708 tests**, with 4 skipped and 88 deselected (251.82 s).
The corrected release reached two Ready API replicas and two Ready controller
replicas. A separate bounded sampler recorded 168 public admin/discovery samples
with zero failed samples. Post-apply plans reported no managed infrastructure,
foundation or workload changes; saved App settings were preserved.

On this image the same serving clone returned the exact expected answer using
a newly issued App-scoped key. Its key was subsequently revoked and returned401;
the owned test user is disabled and the clone has zero actual containers.
All seven Qwen metric families and the Capacity page were verified in loaded
browser screenshots. Historical App/User totals now agree on137 Qwen and28
scientific runs. See `BROWSER-R02.md` and `USERS-CAPACITY.md`.

The scientific clone progressed past discovery but exposed another real failure:
an App-scoped runtime marker disagreed with the immutable image's canonical model
identity. This is preserved as r02, not relabeled as a success.

## Final scientific identity correction

Source `a6963e1fbe70c1043d59a285c0ff2df99d0e0305`, tree
`29603454bb62ee9903804571090eebe86a709fdb`, control-plane digest
`sha256:921f0bb1a91e62df2b35626865ea8c6b7976412ab030bf62f936e86c4f5dd3bf`.
The admin image remains unchanged. The narrow App renderer correction retains
public App identity in capabilities/accounting while supplying the qualified
canonical model identity to runtime markers. No model weights, scientific
parameters, qualified runtime sources or recipes changed. Focused verification:
102 tests, strict typing, Ruff and recipe checks passed.

Terraform applied this exact source; API and controller each reached two Ready
replicas. Only platform image/source-contract references and bootstrap objects
changed. GPU/Kueue quota specifications, priority values and node settings did
not change. The original configuration is retained in private pre-apply receipts.

Scientific r03 completed successfully at13:20 UTC: operation
`a8b7f5ad-4851-468c-8f98-78f50ed03608` ran real CPU/GPU stages, passed semantic
validation and exact-byte artifact downloads. HTTP and named MCP replay returned
the same operation. The independent App retains its prior failed run plus exactly
one new success. Source App history/settings are unchanged; the clone is paused
and both attempt resources are released. `SCIENTIFIC-APPS.md` records details.
