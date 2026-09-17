# SAI-24 protected execution capsule contract

This repository is a source candidate, not an execution authority. Production
release gates must start in an independently installed bootstrap executable;
running a repository Python file, shell helper, Terraform binary, Helm binary,
or `inference-stack` directly is unsupported and must fail closed.

Platform Security owns an external trust document on a root-owned read-only or
fs-verity-backed path outside the checkout. It binds the bootstrap executable,
the complete repository trust document and the complete toolchain lock by
absolute path and SHA-256. The repository copy cannot replace that authority.

Before starting Python, Helm or Terraform, the bootstrap must:

- open every source, trust, signature, closure, inventory, values, chart and
  saved-plan input with no-follow semantics;
- copy each regular-file input into a memfd carrying `F_SEAL_SEAL`,
  `F_SEAL_SHRINK`, `F_SEAL_GROW` and `F_SEAL_WRITE`, then close the pathname;
- load repository Python only from one digest-bound sealed ZIP memfd, with an
  isolated Python distribution whose executable, `sys.path` and package trees
  are all externally bound. `python-entry` imports the measured `security`
  package and calls its `main`; it never executes a repository pathname as
  `__main__`;
- expose `FS2_EXTERNAL_CAPSULE_ACTIVE=1` and `FS2_CAPSULE_SOURCE_ROOT` only to
  that child; the latter names a private, root-owned, read-only exact-tree
  mirror whose hash is in external trust. Protected modules reject direct
  pathname execution before importing any repository sibling;
- import `inference-stack` as a measured sealed module and call
  `main(capsule_fd=...)` with one inherited Unix `SOCK_SEQPACKET` capability.
  The other endpoint remains in the root-owned supervisor. The module verifies
  `SO_PEERCRED`, accepts only UID 0, and receives the exact bootstrap, external
  trust, toolchain, tool-tree and release-contract bindings from that peer.
  The one-shot response must echo the child PID and a fresh 256-bit challenge;
  the inherited descriptor is closed immediately after that handshake.
  Direct execution is permanently disabled; environment flags and repository
  CLI options are not capsule membership and cannot select any binding;
- execute Git, OpenSSL, Helm, kubectl and Terraform only from sealed descriptors
  or an equivalently fs-verity-protected read-only mount, never through `PATH`;
- expose an externally bound read-only tool-dispatch directory for Terraform,
  kubectl, Nebius CLI, crane and every protected-workflow utility. Explicit CLI
  overrides must equal those exact entries; ambient PATH tools are rejected;
- run Terraform `init`, `plan`, `show` and `apply` inside one capsule that uses
  only the lock-authorized provider filesystem mirror; verify every provider
  package, module tree and permitted provisioner executable; deny provider
  network installation and a mutable plugin cache;
- cover the root facade, infrastructure, foundation and workloads roots. Each
  root binds its configuration tree, module graph, provider packages and every
  permitted provisioner executable; the ordinary `inference-stack` entrypoint
  is itself loaded from sealed source and cannot select a different Terraform;
- pass the same sealed binary plan descriptor to both `show` and `apply`, and
  let the in-root gate re-read that descriptor from the actual Terraform
  ancestor before any cluster-dependent resource can run.
- emit a detached-signed plan receipt at plan creation and require it at apply.
  It binds the binary plan, root, configuration and module trees, provider lock
  and packages, permitted provisioners, Terraform executable, external trust
  and toolchain lock. A binary plan without that exact receipt is unusable.

The reviewed bootstrap command surface is closed: `verify`, `python-entry`,
`python-stdin`, `shell-entry`, `exec-tool`, `tool-sha256`, `terraform-init`,
`terraform-validate-root`, `signed-terraform-plan`, `signed-terraform-apply`,
`acquire-release-registry-credential`,
`prepare-workload-registry-secret-leases`, `verify-workload-registry-credential`, `apply-registry-secret`, and
`register-workload-registry-refresh`. The protected scan workflow uses the
toolchain-bound Trivy executable and records that executable's exact digest;
it does not download a second scanner after capsule verification.

The checked-in external-trust document is only a blocked example. Integration
remains **NO-GO** until independent review installs the external authority and
populates all null tool, provider, module, trust, digest, scan and broker facts.

## Short-lived private pulls

Static NVCR environment credentials are not an accepted input. The signed plan
contains only a distinct stable lease identity, lease generation and exact
subject-scope digest for each Secret. Its three ephemeral inputs are distinct,
explicitly invalid noncredential placeholders; no token receipt, expiry, token
revision or Docker bytes are retained in the plan or Terraform state.

Every managed pull Secret also names one signed refresh owner, a maximum
300-second cadence, a pre-expiry rotation margin and non-delete supersession.
The owner contract is `security/workload-registry-refresh-contract.json`.
Its runtime/image/provenance fields and its trust hash are deliberately null;
ordinary private pulls remain blocked until an independently installed owner
is attested, authorized and shown to refresh all three Secrets without putting
long-lived credentials in Terraform state.

The repository never accepts broker receipt, Docker configuration, bootstrap,
trust, toolchain or refresh-registration paths on the `inference-stack` CLI.
After infrastructure and foundation have converged, it asks the authenticated
capsule peer for planning-only render evidence and stable Secret leases. Before
apply, repository code removes the placeholder Terraform variable.
`signed-terraform-apply` must use
the externally bound provider-RPC proxy and
`security/workload-registry-secret-admission-contract.json`: immediately before
starting Terraform it supplies distinct signed noncredential placeholders for
ephemeral expression evaluation; the proxy must reject any placeholder if it
ever reaches the Kubernetes API. Immediately before
each of the three reviewed Secret create/update RPCs, the proxy rechecks that
both refresh owner and proxy readiness were observed no more than 60 seconds
ago, derives exact private subjects from the signed plan and closure, brokers a
new pull-only credential, requires at least 600 seconds remaining, and replaces
only the write-only provider data. It must not change planned annotations or
`data_wo_revision`. A credential may not be reused for another Secret RPC.
After each API response it records a unique token receipt/revision, lease,
subject scope, Secret UID/resourceVersion, response hash and planned/observed
metadata hashes in an external signed handoff. Apply succeeds only after the
capsule verifies the complete one-to-one handoff with the externally bound
signer/public-key/verifier identities; no credential bytes enter it. Those
runtime trust fields remain null in source, so integration stays fail-closed.

## Registry mirroring

There is no mirror-loop credential. Each target lookup, source lookup, copy,
post-copy tag lookup, and digest-reference lookup asks the authenticated
capsule for a new operation-specific authorization. The authorization carries
exact grant rows, each binding one repository+expected-digest subject to one
action and one Docker-auth partition. The capsule rejects flat subject/action
sets, extra auth hosts and any partition whose auth-entry hash differs. Digest calls
have a 240-second execution bound and at least a 300-second TTL safety margin;
copies have a 3,000-second execution bound and the same margin. A copy that
cannot finish within that bound fails closed and is retried as a new operation
with a new authorization; its token is never reused by verification.
