# SAI-19 static remediation handoff

Status: source candidate only; independent review required. This document is
not an integration, deployment, live-verification, rotation, or security-GO
record.

The additive successor starts from rejected commit
`2e11eaf9c502e8f0935b31aa966a6079885805ab` (tree
`96586c6586b3eb2e315a579fccc876aa5760bc16`) and preserves the earlier rejected
`502d0e687e9b22a787b21addd095a7f5397b5b2f`. Those commits remain negative
evidence; this candidate does not reinterpret them as accepted.

## Source boundary

The coordinator authorized static additive source work only. No tests, builds,
formatters, linters, package managers, Terraform, Helm, containers, CI, live
probes, cloud/provider/cluster/database/registry inspection, credential action,
deployment, deletion, cleanup, or resource mutation was executed. Tests were
authored but intentionally not run. The branch must remain integration/live
NO-GO until exact-commit independent review and the parent program's serialized
rollout gates accept it.

## Candidate design

- The shared gateway mounts no provider key and no authority signing key. A
  dedicated Ed25519 issuer alone mounts the private key; tenant brokers mount
  only the public key. The issuer authenticates the gateway with an exact
  Kubernetes TokenReview endpoint and re-derives executor, workload, or
  tenant-scoped operator scope from durable records before signing. Brokers
  repeat durable-state checks before use. The gateway ledger HMAC and shared
  token pepper are not capability signing material: they only protect ledger
  values or verify an existing raw PAT/session proof. The gateway cannot mint
  the Ed25519 artifact capability checked by the brokers.
- Serving input/output workers use fenced executor claims; scientific workload
  routes use active attempt claims; terminal scientific result publication uses
  a separate exact-artifact read claim derived from the still-active batch
  controller lease. The issuer derives the controller ID and fencing token
  from PostgreSQL rather than a gateway echo, and the tenant broker rechecks
  that same unexpired lease before storage access.
- Ordinary Terraform declares one provider identity, exact-prefix group,
  access key and immutable Secret per tenant and retained generation. One
  tenant broker mounts only the selected active generation. The pre-SAI-19
  shared identity, group, access key and Kubernetes Secret keep their exact
  addresses with `prevent_destroy`. The mandatory reversible
  `legacy-overlap` phase retains the old prefix authorization because
  infrastructure applies before the replacement workloads. Ordinary applies
  cannot select a one-way cutover phase and render no cutover Job/controller;
  reserved caller-supplied digest fields must remain empty.
- Rotation inputs are fail-closed as well: every retained generation remains
  authorized during `legacy-overlap`, and generation 1 must remain active.
  Later generations may be prepared additively, but no source path currently
  permits switching active traffic or deauthorization. Both remain blocked on
  a separately reviewed, purpose-bound signed readiness/drain and provider-IAM
  receipt design; Deployment readiness or the broker identity echo is not that
  evidence.
- A full-stack destroy is intentionally blocked in both bucket modes. Lifecycle
  outputs distinguish the separately eligible empty disposable bucket from the
  protected identity generations that require explicit state adoption or a
  future independently reviewed retirement.
- Broker routing accepts the independently authorized tenant plus the exact
  repository row. It exact-matches tenant, operation, attempt, digest,
  canonical key, action and immutable provider version. The provider endpoint
  is fixed to `https://storage.<region>.nebius.cloud`; there is no
  operator-supplied STS/exchange URL and projected tokens are sent only to
  exact in-cluster services or the Kubernetes TokenReview endpoint.
- The prior in-process static tenant credential loader is retained only as
  history and fails closed before reading a directory; neither the runtime
  settings nor CLI contains a switch that can select it.
- Artifact-disabled deployments construct neither the credential-broker client
  nor the authority-issuer client. Their projected token, CA, and readiness
  mounts remain artifact-only, so the default disabled control plane does not
  initialize an unused TLS transport against an absent file. When artifacts
  are enabled, broker metadata calls, proxy uploads, and content streams use
  the declared 30-900 second operation timeout while connect and pool waits
  remain on the shorter broker timeout; readiness calls retain the short
  control-plane timeout.
- Every broker operation performs a provider-backed bucket-versioning and exact
  tenant-prefix visibility probe before returning a handle, receipt, or byte
  stream. Broker responses bind the configured credential generation and
  provider-binding digest, and gateway/backfill clients compare those values
  with their mounted routing inventory. Kubelet readiness additionally probes
  provider access and the broker database. These checks detect stale routing;
  they are not authoritative provider-wide IAM enumeration or a future
  generation-activation receipt.
- Public finalize accepts the provider-returned `VersionId`. Existing HTTP and
  MCP clients may omit it; the broker then permits only bounded discovery of a
  unique write-once version and still verifies that exact version before
  finalization. The version is recorded before a separately authorized exact-
  version GET verifies digest, size, media type and compression. Downloads
  sign that immutable version.
  Small service-mediated reads buffer and verify before releasing any byte;
  public large artifacts use the version-pinned handle without a preliminary
  full GET. Background model-input materialization uses the separately
  declared field byte ceiling, so a valid 32 MiB input is not narrowed by the
  16 MiB public inline-response ceiling; it still buffers and verifies the
  exact version before exposing bytes to the runtime.
- Direct upload responses name `x-amz-version-id` as the required version
  response header. The gateway content path returns the provider version as
  `x-fs2-object-version-id`. Cross-origin direct upload remains integration-
  gated on exact-origin CORS that exposes `x-amz-version-id`; without that
  independent proof, browser clients must use the gateway content path and no
  latest-key fallback is permitted.
- Public signed PUTs bind checksum, `If-None-Match: *` and exact content
  length. After a one-hour grace, ordinary maintenance appends a finalization
  fence, discovers only an unambiguous single current version using HEAD plus
  a bounded exact-prefix version listing, and records an issuer-signed exact-
  version quarantine receipt without deleting provider bytes.
  Standalone claim/version/attempt/receipt evidence survives operation-row
  retention; attempt events rotate unresolved claims fairly. An absent upload
  is sealed with an exact provider conditional-write fence and an issuer-signed
  receipt, so a PUT accepted near URL expiry cannot appear after its metadata
  was purged.
- Tenant provider identities have only `storage.uploader`,
  `storage.object-viewer`, and `storage.object-lister`; they never receive
  `storage.object-editor`/DeleteObject. Bucket versioning is enabled and no
  lifecycle rule expires noncurrent versions. This preserves the exact pinned
  VersionId if a compromised uploader creates a newer canonical-key version.
- Historical finalized rows without a version stay fail-closed and fence the
  whole operation purge. A maintenance Job accepts an explicit artifact/version
  manifest, hashes the exact version, verifies immutable metadata and invokes
  the single receipt-backed NULL-to-version transition. No latest-key backfill
  exists.

## Deliberately unresolved integration gates

This source remains NO-GO. In particular, it still uses retained long-lived
per-tenant provider keys, does not provide authoritative provider-wide IAM/key
enumeration, does not bind executor/controller/workload issuance to distinct
actual actor identities rather than the shared gateway, and does not yet have
a cryptographically verified multi-observer activation receipt. The
compatibility bridge therefore stays open. Exhaustive legacy input enrollment
for ordinary online/MCP artifact-backed operations has not been proved and the
one-way database close must not run. Historical-version backfill still starts
from an operator-supplied manifest and is not an authoritative exhaustive
database/provider census. Provider retention deletion and abandoned-upload
reconciliation are also dormant: tenant brokers have no delete role, and
expired or unfinalized bytes and metadata are retained until distinct reviewed
authorities and provider-backed quiescence evidence exist. These are blockers,
not residual-risk acceptances.

## Required independent verification

Review the exact candidate tree before integration. At minimum, independently
exercise: asymmetric signer/verifier custody; tenant and foreign-prefix
denials including in-prefix delete denial; official endpoint binding; a staged
generation handoff with N-1 and N overlap; public upload/finalize; background input/output;
tenant-scoped admin-debug download; exact-version corruption and TOCTOU cases;
abandoned upload absence fencing, discovery, crash-resume, quarantine and fairness; a
historical backfill canary; whole-operation purge fencing; migration/role
contracts; chart and ordinary Terraform rendering; and zero-delete/no-replace
plans. Record old/new deployment identity and rollback evidence only in the
separately authorized integration/live lane.
