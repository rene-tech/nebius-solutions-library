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
`acquire-workload-registry-credential`, `verify-workload-registry-credential`, `apply-registry-secret`, and
`register-workload-registry-refresh`. The protected scan workflow uses the
toolchain-bound Trivy executable and records that executable's exact digest;
it does not download a second scanner after capsule verification.

The checked-in external-trust document is only a blocked example. Integration
remains **NO-GO** until independent review installs the external authority and
populates all null tool, provider, module, trust, digest, scan and broker facts.

## Short-lived private pulls

Static NVCR environment credentials are not an accepted input. The capsule
joins broker-returned Docker bytes to their detached-signed receipt and release
closure, then projects the bytes only through Terraform ephemeral variables and
provider write-only Secret fields. The non-secret projection carries the exact
repository+digest subjects, receipt revision and expiry.

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
capsule peer for a new exact-subject workload credential immediately before the
workload plan/apply pair. Admission requires the independently attested refresh
owner to be live and observed, and requires at least 600 seconds of credential
lifetime at both plan and apply entry. If planning consumes that margin, apply
fails before mutation and a new plan must be created with a fresh credential.
