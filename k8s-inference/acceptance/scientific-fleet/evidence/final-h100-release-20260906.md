# Deployed H100 release — 6 September 2026

The H100 cluster remains running for customer testing. Final deployed source is
`adf1d8423e9b75afc5ba208baf53e479e7793922`, tree
`cceaf4edb27cbbc000e5490279ed218d27923896`. Subsequent documentation commits
record this deployment; they do not imply a different running image.

## Accepted functionality

- All ten scientific profiles passed real requests with semantic results and
  verified artifacts in the [final fleet campaign](final-fleet-acceptance-h100-20260906.md).
  Final catalog promotion preserves all execution identities and adds ten
  immutable scheduler-eligibility receipts. No intermediate unqualified catalog
  was deployed.
- Qwen and Cosmos passed the [current general canaries](../../general-serving/evidence/h100-8bb53aab-canary-20260906.md).
- [Live customer access and dispatch policies](customer-access-policy-h100-8bb53aab-20260906.md)
  passed scoped key issuance/revocation, authorized discovery, paused durable
  queues, resume, cap-one scheduling, completed outputs and original-policy
  restoration. Earlier real browser checks cover cancellation, general-model
  settings and observability links.
- The deployed MCP transport lists all ten scientific aliases and both general
  models with TLS verification and principal-private, zero-TTL discovery.
  Replaying the completed ESMFold2-Fast request through its named alias preserved
  its operation and two attempts, returned valid artifacts, and launched no
  new GPU work.
- Public authenticated overview, models, capacity, configuration, scientific
  policy and scientific discovery endpoints returned HTTP 200. Overview,
  models, capacity and configuration reported no source warnings. The false
  H100 placement warnings were absent in both API data and the actual browser.
- API, controller and admin rollouts succeeded. A subsequent read observed
  API 3/3, controller 2/2 and admin 2/2 ready; the API HPA operated within its
  unchanged configured ceiling.

## Terraform and access handoff

The exact committed source was built into regional images and deployed through
the staged Terraform entrypoint using the existing H100 `terraform.tfvars`.
The final post-apply plan converged across infrastructure, foundation and
workloads: **zero resource actions and zero output changes in all three**.
Its private log SHA-256 is
`f9dcdbb25541d6dbdbfbec8261dbf751f8df1401d9a29a428b4ddeaf03c8e7a1`.

The access bundle was refreshed through `inference-stack output`. Admin, MCP,
scientific and Grafana credentials were present and unchanged. The output
remains owner-readable only; the previous bundle is retained as a backup.
Public admin, MCP, inference and observability locations are in that same
bundle; no tunnel or port forwarding is necessary. See the
[access guide](../../../docs/ADMIN_OBSERVABILITY_ACCESS.md) for field names.
Never put these credentials into source control or public evidence.

Release OCI index identities:

| Component | SHA-256 |
| --- | --- |
| Control plane/controller | `4134c130d4d265212886b0a17a03b0b8051d01bd592032b00509ed657378623a` |
| Admin console | `efb9750399e9b17175559fece20585b56fc06c839a428f6db615075c6d3f4857` |
| Admin CycloneDX SBOM | `2dfa55eb1f639e4d389d53dae58304c620c99d90b4f9c6e98fc6e6749e551ef7` |

The only physical capacity adjustment during this review was the existing
batch CPU pool's minimum from zero to one, within its unchanged maximum of two.
Reserved H100 capacity, preemptible GPU envelopes, model resource requests,
limits and cloud quotas were not increased. No B300 resources were changed.
Temporary scientific acceptance workloads drained; the separately documented
experimental checkpoint PVC is retained, without a running probe.

## Regression results and limits

The integrated backend suite on `1cc53e34` passed 1,426 tests with 75 optional-service
skips; the admin suite passed 136 tests and TypeScript checks. Strict mypy checked 104
source files, and Ruff passed. The final MCP/configuration combination passed
61 focused tests. Real PostgreSQL policy/lifecycle tests provide separate live
database coverage; optional-service skips are not counted as those checks.

On exact final source, recipe verification, 36 scientific contract tests,
38 fleet harness tests, 10 primary activation tests and adapter checks passed.
The complete solution suite passed 306 of 307 tests. The remaining
public-export reference check reports documentation paths and historical
resource IDs; it is recorded in the [solution gate report](solution-acceptance-gates-20260906.md),
not suppressed or presented as a functional deployment failure.

This release is not a claim of universal production GPU snapshot support,
untested GPU-family qualification, maximum-throughput or cold-start p95
qualification. The [operator guide](../../../docs/SCIENTIFIC_READINESS_AND_OPERATIONS.md)
separates measured improvements from remaining costs. On H100, warm same-GPU
experimental ESM checkpoint restores were fast, but a genuine disk-cold restore
took 324.646 s. Normal production loading remains the default. Some CPU model
images still incur 138–149 s cold pulls. Academic model entitlements still apply.

Private final public-surface receipt SHA-256:
`e07f6e28d2be216df583d7d3f6ddd03af474a0603b66c09a4894cf827a781244`.
Private final MCP-discovery receipt SHA-256:
`52433b2e2d1b71bcff6a1cba7ef642605a66fb38ec31deeb4cc9b18072627ec8`.
