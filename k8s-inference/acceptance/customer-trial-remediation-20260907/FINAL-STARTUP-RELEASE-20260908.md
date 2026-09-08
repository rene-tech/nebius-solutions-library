# Final startup-retention correction

Source: `c85aa26e46f84ca5ae0a85b2454e86522b65cead`, pushed to
`rene-tech/nebius-solutions-library` main. Source tree:
`5d3b4fa7ec4689b92e1f62b31ac9b9119c250bc6`.

Status at 08:58:48 UTC: staged Terraform deployment completed. All three
application Deployments have two updated/ready/available replicas, observed
generations and zero terminating replicas, with the exact images below.
Public discovery/admin inventory retains all 24 required configured models;
see [the inventory receipt](final-startup-deployed-inventory.json).
This checks configuration/discovery, not new inference proof for all 24 models.
The generated Qwen ScaledObject is Ready and contains the final missing-Ready
handling and `900s:1s` timestamp history. Post-apply infrastructure, foundation
and workloads plans each have zero managed actions. Fresh r04 started at
09:01:13.093375 UTC with all four live test lanes; no clean full-cohort
acceptance is claimed yet.
The previously built partial candidate `208a2e20` was not deployed.

| Component | Published digest |
|---|---|
| Control plane/model controller/scientific companions | `sha256:8c05c7a62e439ad01cb982976c1ce6203596d3b27647a67e21bb5d9792af2964` |
| Admin console | `sha256:0f2b63c40d67c778cb2fc272a7eb482bc170b786ab257fbebe0461d7658291bc` |

Admin CycloneDX SBOM SHA256:
`2af522465f1720d4d22e7e550002c1ff8e0ca035592d19faf46c882ecc73ef94`.
Package-lock SHA256, unchanged:
`1d45f7fa8912c7801a02d02ecfb75c6c69281ac1ce6ea5135851250a547eda45`.

## Scope and verification

Owned, nonterminal Pods without a Ready series now retain startup demand.
The positive desired-replica scrape timestamp is preserved across off-grid
scrapes, without renewing the timeout or creating demand from zero replicas.
See the [mechanisms and failed intermediate evidence](observer/QWEN-UNSCHEDULED-STARTUP-20260908.md).

- Final complete non-external backend: 1,633 passed, 4 optional skips,
  77 external deselections, 230.33 seconds. Existing Starlette deprecation and
  unrelated pytest temporary-directory cleanup warnings are retained.
- Focused suite: 145 passed, including actual Prometheus expression tests.
  Independent verification confirms sustained protection through 949 seconds
  and expiry at 950 seconds for a positive scrape at 50 seconds and a 900-second
  budget, with both 5- and 10-second off-grid scrape fixtures.
- Two real historical unscheduled-Pod cases return retained demand before
  their recorded cancellations. Before telemetry observes the new Pod/count,
  demand correctly remains zero. These are historical checks, not live passes.
- The private customer tfvars changes only two image pins and three matching
  provenance values. No GPU quota, node-group limit, startup budget, hot floor,
  scientific fixture, qualified runtime recipe, driver or model policy changes.

R04 and r05 will use the unchanged fourteen-operation scenario, ordinary
HTTP/MCP traffic, cluster observation, actual admin browser publication/download
checks and complete-window serving publication logs. The browser uses a local
isolated network namespace to avoid unrelated test-host virtual-interface
notifications; no application behavior or browser network-error checking is
disabled. Earlier failed cohorts remain preserved.

The reviewed pre-apply plan had zero infrastructure actions, three expected
foundation contract actions and 24 workload actions. Every queue, flavor,
cohort and priority manifest's non-metadata content was unchanged. Helm values
changed only application pins/provenance and generated contract names/hashes;
existing immutable bootstrap ConfigMaps/Jobs were refreshed by the normal
workflow. No customer result or persistent store was removed. Private original
plans and the original plan log are retained under `releases/c85aa26e/preapply`.
Rollback images remain documented in the preceding release evidence: control
plane `sha256:762510cb5354dea8f9d32834259dd164d561137582f4dd04aca18af4263b57bc`
and admin `sha256:fc7b0f2f8207eebc28576f55e9815de92a5c04bfb809beafb5c78ac5fae8b59c`.

Post-apply private plan JSON SHA256s:

- Infrastructure: `450e87a3c72ca85dae804b88e2822d7147e11575e13da03cbc3ef8fac9c412d3`.
- Foundation: `598603a67a55302b1df313d883ccfc7e4f85ae30865bbd2c06c3c207cfe51aed`.
- Workloads: `13954033f0479f889d97373a3c95bf4da30a771d0dd8a4ed5ae14032b4c217b5`.
