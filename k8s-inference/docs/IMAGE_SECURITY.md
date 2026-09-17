# Image vulnerability and dependency pinning policy

This policy is a source candidate for SAI-24. It does not constitute a scan,
image publication, integration approval, remediation claim, or deployment
approval.

## Release gate

Every first-party and third-party runtime image must be identified by an OCI
`sha256` digest and scanned for vulnerabilities and secrets before promotion.
A candidate is rejected when it contains a fixable `CRITICAL` or `HIGH`
finding, or any secret finding. The scan receipt must bind the source commit,
source tree, image repository and digest, scanner/database identity, command,
timestamp, and complete result counts. A tag-only result is not evidence for a
deployed image.

The image-security workflow is a required reusable dependency of normal
Kubernetes inference CI as well as being scheduled for 03:17 UTC daily. Pull
requests run only an untrusted, unsigned source scan and never receive signing
authority. Non-PR release runs additionally require the protected
`sai24-release-attestation` environment, GitHub's short-lived OIDC identity,
and an authorized signing broker; no long-lived signing key is inherited or
written to the runner. The untrusted lane installs a specific Trivy archive
only after checking its SHA-256. The protected lane instead executes the exact
Trivy bytes bound by external capsule trust and records that executable digest
in every promotion receipt. Both emit complete JSON results and SPDX JSON
SBOMs and retain their receipt bundles for 90 days. The untrusted lane scans
the control-plane and admin source candidates and catalog filesystem. Its
receipts are explicitly marked `untrusted-source-scan-only` and promotion
validation rejects them. The protected lane separately scans every
production digest in the render-derived release closure, including first-party
control/admin images, chart hooks/init containers, and mapped catalog model
runtimes. A new critical result has a 24-hour remediation SLA; a
new high result has a seven-day SLA, measured from the scanner result
timestamp. A failed or missing scan, incomplete inventory, tag-only subject,
or missing signed receipt blocks promotion rather than extending the SLA or
converting the result into a pass. First-party receipts bind the BuildKit
provenance, retained OCI archive, manifest digest, commit, and tree. All scan
receipts and the exact render packet require a detached signature under the
independently reviewed Platform Security trust root. Local source-build scan
subjects are explicitly not accepted as substitutes for rendered production
digests.

The protected scan derives the registry set from that exact closure. Every
registry must be explicitly classified in the trust policy. Private production
registries and `nvcr.io` use a protected-environment OIDC exchange whose request
and response authorize only the exact repository, manifest digest, and `pull`
action for each closure subject. The returned host entry merely transports that
claim-bounded token; it is not host-wide authority. PR jobs never receive it,
and no static registry credential is inherited. An NVCR subject without that
protected authentication classification fails before scanning.

## SAI-24 source changes

- The control-plane runtime installs `libuuid=2.41.6-r0`, the fixed package
  version identified by the review, alongside its existing exact Alpine
  security package pins.
- The admin-console runtime refreshes only `libcrypto3`, `libssl3`, `libexpat`,
  and `libuuid`, and accepts them only as exact `name=version` constraints from
  `security/alpine-runtime-packages.lock.json`.
- The catalog runtime moves from vulnerable `cryptography==41.0.7` to the
  repository's already locked `cryptography==50.0.1` artifact set.
- Every Nebius Terraform root uses the exact provider constraint `= 0.5.232`
  instead of the unbounded `>= 0.5.232` constraint.
- Every Terraform Helm release and direct add-on, bootstrap, and observability
  installer consumes both `security/third-party-images.lock.json` and
  `security/first-party-images.lock.json` through the same fail-closed
  post-renderer and validates their signed authority against
  `security/image-attestation-trust.json`. It rewrites reviewed third-party
  tags to digests, accepts exact attested first-party digests, and rejects every
  unknown tag or unlisted digest; observability is not removed.
- The post-renderer does not treat its span lexer as a YAML parser. A separately
  hash-pinned PyYAML 6.0.2 parse must produce the same image multiset, and the
  semantic pass rejects aliases, anchors, merge keys, duplicate keys and
  non-scalar image values before any rewrite.
- The same source-derived installer discovery includes separate `helm install`
  and `helm upgrade` forms. The shipped ModelExpress helper and its test hook
  therefore pass through the post-render gate instead of retaining a tag-only
  side path.
- First-party locks reference small signed build attestations containing the
  exact source, Dockerfile, manifest, BuildKit provenance, OCI archive, SBOM,
  report, scan-receipt, retained-artifact hashes, and retention deadline. The
  large OCI archive remains in the 90-day evidence store rather than Git; the
  protected closure job independently scans the production digest again.
  Validation opens the retained OCI archive, verifies the current checkout and
  Dockerfile, verifies report/SBOM/receipt contents and signatures, verifies a
  signed OCI referrer set points to those same payloads, requires a signed
  image-identity payload, opens every retained OCI referrer manifest, and
  matches the complete descriptor set to the body of an authorized, signed
  registry Referrers API query receipt. A locally assembled descriptor list is
  not registry-attachment evidence. The validator also checks the exact
  artifact remains within a non-expired 90-day retention interval.
  Each accepted first-party attestation therefore names retained
  `registry_referrers_response`, `registry_referrers_query_receipt` and
  detached receipt-signature artifacts, plus the signed image payload. The
  receipt binds the exact registry, repository, digest-specific Referrers API
  path, HTTP 200 response-body hash, authorized resolver, and protected OIDC
  identity.
- `security/release_image_closure.py` discovers the Terraform Helm resources
  from source, checks all direct installers, and requires an out-of-tree signed
  render packet. Every render provenance record binds an authorized builder,
  the exact chart archive and its `Chart.yaml` name/version, ordered values,
  the hashed Terraform resource or direct-install declaration, post-renderer
  and lock identities, and rendered YAML. The packet source commit/tree must
  equal the checkout, but the packet is never checked into that tree, avoiding
  a self-referential Git identity. A checked-in boolean cannot assert
  completeness.
  Terraform surfaces are additionally joined to the exact planned
  `helm_release` address, planned values bytes, release name, namespace,
  repository and version, and undeclared planned Helm resources are rejected.
  Direct installers are discovered across every shell entrypoint regardless of
  filename; workflows, Makefiles, and operator documentation are also searched
  for raw install commands. Non-release test fixtures require an explicit
  purpose ledger. ModelExpress additionally verifies the signed release name,
  namespace, install/upgrade mode, chart source tree, ordered values and actual
  post-rendered manifest before any cluster mutation. The production Terraform
  roots require a closure contract and place a signed plan-closure gate before
  their cluster contract. `security/apply_signed_terraform_plan.sh` is only a
  sealed-shell compatibility dispatcher. The independently installed capsule
  copies the binary plan, closure, signatures, trust, configuration, providers,
  modules and tools into sealed descriptors/read-only mounts, renders and
  applies the same plan descriptor, and permits cluster mutation only after
  every planned Helm resource matches the closure. Root facade and
  infrastructure plans use the same capsule even though they own no Helm
  release; no ordinary Terraform apply path remains.
  A caller may select a candidate evidence path, but cannot replace the
  source-pinned trust authority or authorize values absent from signed evidence.
- `security/catalog-images.lock.json` maps `registry.example.invalid` source
  identities to real production repositories while preserving the manifest
  digest. Every mapping and every third-party tag resolution requires a signed
  semantic receipt whose manifest bytes hash to the OCI digest, whose registry
  matches the repository, and whose resolver identity is explicitly allowed by
  the trust policy.
  Root Terraform consumes only the reviewed checked-in map before selected
  runtime references reach workload, keeper, pre-pull, controller, or
  CPU-runtime consumers; the compatibility path input cannot replace that
  authority. An out-of-tree evidence copy must be byte-identical to the source
  map. Missing mappings fail closed, and non-placeholder customer image
  overrides are preserved rather than silently remapped. The checked-in
  blocked template is never a production mapping.

The post-renderer accepts protected out-of-tree inventories only when a signed
materials authorization (during rendering) or signed final release closure
(during apply) binds their exact hashes to the current clean commit/tree and
the source-pinned trust policy. Caller-supplied hashes are not authority. The
checked-in materials document authorizes nothing. The repository trust policy
and toolchain lock are subordinate to the external root-owned, read-only
capsule authority; an environment variable can only name that exact externally
bound object, not replace it. This keeps source templates fail-closed
while preserving the install path once the external evidence packet has been
independently accepted.

First-party custody validation opens the provider archive and requires its
member set and bytes to equal the declared reports, SBOM, provenance, receipts,
signatures and OCI artifacts. A separately signed provider receipt binds the
raw GitHub artifact API response, archive bytes, workflow run, repository and
90-day expiry under an authorized protected observer. Build provenance is
matched as structured in-toto/SLSA fields—OCI subject, Git commit/tree,
Dockerfile path/hash and authorized builder—not by substring search.

## Required integration work

The coordinator boundary for this source candidate forbids registry access,
image pulls, builds, and scanners. Consequently, the admin package revisions;
the website Node base digest; and the CNPG, Envoy Gateway,
kube-prometheus-stack, Loki, Tempo, and OTel image digests are intentionally
not guessed. The DCGM entry retains the exact digest already present in the
current source tree, but it is not accepted without the new resolution
provenance and signed render/scan evidence; no rejected historical digest
packet is reused. An authorized integration worker must render every exact
chart+values surface, produce the structured render-provenance records and
signed packet outside the Git tree, record signed registry-resolution and
catalog-mapping provenance, capture each production subject's registry
Referrers API response plus authorized signed query receipt, populate the
package and image locks, and configure
the independently reviewed trust key, allowed builders/resolvers, protected
workflow refs, evidence artifact identity, and OIDC broker. The protected
workflow then scans every derived production closure subject and signs and
retains its reports, SBOMs, closure, and receipts for 90 days. Until those
currently null/blocked values are independently resolved, this candidate
remains **SOURCE NO-GO**. The installers remain present and become usable once
those mandatory inputs are accepted; no tag/default path can bypass the
post-render gate.

All protected Python and shell entrypoints are loaded by the external capsule,
not by pathname. The capsule verifies external root-owned trust before loading
one sealed Python package or shell source, exposes an exact read-only source
tree, seals each plan/evidence input, and binds the root, infrastructure,
foundation and workloads Terraform lifecycles to the same provider/module/tool
graph. Repository-local launchers are non-authoritative fail-closed stubs.

Private workload pulls additionally require a signed refresh registration.
The accepted refresh owner must rotate each exact repository+digest pull token
before its <=900-second expiry, retain the last valid revision on failure,
audit every transition, and supersede without deleting the Secret. Static
Docker config environment variables and host credential merging are rejected.
The checked-in refresh controller contract has null runtime/provenance fields,
so private-image integration remains blocked rather than pretending that one
short-lived initial Secret preserves later scale or reschedule behavior. The
separate provider-RPC admission contract is also blocked until Platform
Security supplies its exact proxy executable, provider-protocol, SBOM and
provenance identities, plus the external handoff signer/public-key/verifier
identities. The signed plan stores a distinct stable lease and lease
generation for each Secret, plus only invalid ephemeral placeholders. The proxy
must preserve every planned annotation and `data_wo_revision`, broker distinct
fresh bytes immediately before each exact Secret create/update, and reject
readiness older than 60 seconds. Each volatile token receipt/revision is bound
to the API response in a complete signed external handoff; Terraform apply may
not report success until the capsule verifies that handoff.

Regional mirroring similarly has no credential shared by an artifact loop.
Every digest lookup and copy receives a distinct operation ID and exact grant
rows. Each row binds one repository+expected-digest subject to its action and
Docker-auth partition; flat subject/action cross-products are forbidden. The
capsule compares each Docker auth entry hash and the complete host set to those
rows, applies a bounded command timeout and TTL safety margin, and uses a new
authorization for every post-copy check.

Rollback is a normal Git revert of the integration commit plus restoration of
the previously recorded first-party image digests or Helm revisions. The
integration packet must capture those previous identities before any rollout.
