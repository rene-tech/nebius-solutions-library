# Final startup-retention correction

Source: `c85aa26e46f84ca5ae0a85b2454e86522b65cead`, pushed to
`rene-tech/nebius-solutions-library` main. Source tree:
`5d3b4fa7ec4689b92e1f62b31ac9b9119c250bc6`.

Status at 08:54 UTC: exact-image builds and regression tests passed. Staged
Terraform deployment is in progress; no live acceptance is claimed yet.
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
